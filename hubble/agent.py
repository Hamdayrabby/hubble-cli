"""Agent loop: model call -> tool calls -> permission check -> execute -> feed results -> repeat.

The loop never prints. It reports progress through an `Events` object so the same loop
drives the interactive REPL, headless `-p` mode and read-only sub-agents.
"""

import itertools
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hubble.hooks import HookRunner
from hubble.permissions import Permissions
from hubble.prompts import COMPACT_PROMPT, build_system_prompt, load_memory
from hubble.provider import OpenAICompatProvider, ProviderError, ToolCall, TurnResult, parse_arguments
from hubble.session import Session, repair_history
from hubble.skills import Skill, SkillTool, WriteSkillTool, discover_skills, skills_prompt_block
from hubble.tools import (READ_ONLY_TOOL_NAMES, Tool, ToolContext, ToolError, default_tools, run_tool,
                            shell_name, truncate)


def mcp_tools(settings: Dict[str, Any], root, events=None) -> List[Tool]:
    if not settings.get("mcp_servers"):
        return []
    from hubble.mcp import load_mcp_servers
    return load_mcp_servers(settings, root, events)


def web_tools(settings: Dict[str, Any]) -> List[Tool]:
    if settings.get("web_tools") is False:
        return []
    from hubble.web import WebFetch, WebSearch
    return [WebSearch(settings.get("web_search") or {}), WebFetch()]


def estimate_tokens(text: str) -> int:
    return int(len(text) / 3.8) + 1 if text else 0


def estimate_messages(messages: List[Dict[str, Any]]) -> int:
    from hubble.images import IMAGE_TOKEN_ESTIMATE, content_text, image_count
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            total += 4 + estimate_tokens(content_text(content).replace("[image]", "")) + \
                IMAGE_TOKEN_ESTIMATE * image_count(content)
            continue
        total += 4 + estimate_tokens(content)
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(tc["function"]["name"] + tc["function"]["arguments"])
    return total


class Events:
    """No-op base. The UI overrides what it needs."""

    def turn_start(self): pass
    def text(self, delta: str): pass
    def reasoning(self, delta: str): pass
    def turn_end(self, result: TurnResult): pass
    def tool_start(self, tool: Tool, args: Dict[str, Any]): pass
    def tool_result(self, tool: Tool, args: Dict[str, Any], output: str, is_error: bool): pass
    def notice(self, message: str, level: str = "info"): pass
    def todos(self, todos: List[Dict[str, str]]): pass
    def batch_start(self, count: int): pass
    def subagent_start(self, key: str, label: str): pass
    def subagent_step(self, key: str, action: str, is_tool: bool = True): pass
    def subagent_tokens(self, key: str, tokens: int): pass
    def subagent_end(self, key: str, status: str, detail: str = ""): pass
    def batch_end(self): pass

    def ask(self, tool: Tool, args: Dict[str, Any], preview: Optional[str]) -> Tuple[str, str]:
        """Returns (answer, feedback); answer is yes | always | no. Raise KeyboardInterrupt to stop the turn."""
        return "no", "Tool calls that need approval are denied in non-interactive mode."


@dataclass
class RunStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    interrupted: bool = False
    hit_step_limit: bool = False
    error: Optional[str] = None


class Agent:
    def __init__(self, provider: OpenAICompatProvider, settings: Dict[str, Any], ctx: ToolContext,
                 permissions: Permissions, events: Events, session: Optional[Session] = None,
                 tools: Optional[List[Tool]] = None, system_override: Optional[str] = None):
        self.provider = provider
        from hubble.providers import normalize_provider_name
        self.provider_name: str = normalize_provider_name(settings.get("provider"))
        self.fallback_client: Optional[OpenAICompatProvider] = provider
        # (provider_name, model) -> (client, fallback_model) or None; set by the REPL/headless runner.
        self.fallback_resolver = None
        self.settings = settings
        self.ctx = ctx
        self.permissions = permissions
        self.events = events
        self.session = session
        self.model: str = settings["model"]
        self.persona: str = settings.get("persona", "code")
        self.temperature: float = float(settings.get("temperature", 0.3))
        self.system_override = system_override
        self.messages: List[Dict[str, Any]] = []
        self.pinned: Dict[str, str] = {}
        self.memory = load_memory(ctx.root)
        self.skills: List[Skill] = discover_skills(ctx.root) if tools is None else []
        from hubble.subagents import discover_agents
        self.agent_defs = discover_agents(ctx.root) if tools is None else []
        self.tools = tools if tools is not None else (
            default_tools() + web_tools(settings) + mcp_tools(settings, ctx.root, events)
            + [TaskTool(self), WriteSkillTool()] + ([SkillTool(self.skills)] if self.skills else []))
        self.hooks = HookRunner(settings.get("hooks") or {}, ctx.root, ctx.shell_argv, events)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.context_tokens = 0
        self.last_stats = RunStats()
        # Shared with sub-agents so Ctrl+C stops every thread of a parallel batch.
        self.cancel = threading.Event()
        self.is_subagent = False
        self.session_context = ""  # from SessionStart hooks; part of the system prompt
        self._session_started = False
        from hubble.stats import StatsLog
        self.stats_log = StatsLog(enabled=settings.get("stats", True) is not False)
        # provider name -> API client; set by the REPL/headless runner so routing can switch provider.
        self.client_for = None
        self.last_route = None     # (tier, provider, model, reasons) of the last routed prompt
        self._route = None         # per-task routing state while a routed task runs
        self._served = None        # (provider, model, via_fallback) of the last successful call

    def start_session(self, source: str = "startup"):
        """Run SessionStart hooks once. source: startup | resume | clear."""
        self._session_started = True
        res = self.hooks.run("SessionStart", {"source": source,
                                              "session_id": self.session.id if self.session else None})
        self.session_context = res.additional_context

    def shutdown(self, reason: str = "exit"):
        """Run SessionEnd hooks and stop any MCP server subprocesses. Call once, on exit."""
        if self._session_started:
            self.hooks.run("SessionEnd", {"reason": reason,
                                          "session_id": self.session.id if self.session else None})
        from hubble.mcp import stop_mcp_clients
        stop_mcp_clients(self.tools)

    # ----- state helpers -------------------------------------------------

    @property
    def tools_by_name(self) -> Dict[str, Tool]:
        return {t.name: t for t in self.tools}

    def system_prompt(self) -> str:
        if self.system_override:
            return self.system_override
        from hubble.sandbox import effective_mode
        mode = effective_mode(self.ctx.sandbox)
        if mode == "docker":
            shell_label = (f"an isolated Docker container (image {self.ctx.sandbox_image}), reached via "
                          f"`sh -lc`; use POSIX/Linux syntax regardless of the host OS. Only /workspace "
                          f"(this project) is visible inside it"
                          + ("" if self.ctx.sandbox_network else "; it has no network access"))
        elif mode == "native":
            shell_label = (f"{shell_name(self.ctx.shell_argv)}, inside a sandbox: commands can read anything "
                           "but write only inside the workspace and temp dirs"
                           + ("" if self.ctx.sandbox_network else ", with no network access")
                           + ". If a command must write elsewhere (global installs, ~/ config), set "
                           "unsandboxed: true on that one shell call; the user will be asked")
        else:
            shell_label = shell_name(self.ctx.shell_argv)
        prompt = build_system_prompt(self.ctx.root, self.persona, shell_label, self.model,
                                     self.pinned, self.memory, self.permissions.mode,
                                     skills_prompt_block(self.skills))
        if self.session_context:
            prompt += f"\n\n<session-start-context>\n{self.session_context}\n</session-start-context>"
        return prompt

    def reload_skills(self):
        """Re-scan skill files and refresh the skill tool (called after write_skill saves one)."""
        self.skills = discover_skills(self.ctx.root)
        for t in self.tools:
            if isinstance(t, SkillTool):
                t.by_name = {s.name: s for s in self.skills}
                return
        if self.skills:
            self.tools.append(SkillTool(self.skills))

    def reload_agents(self):
        from hubble.subagents import discover_agents
        self.agent_defs = discover_agents(self.ctx.root)

    def reload_memory(self):
        self.memory = load_memory(self.ctx.root)

    def _append(self, message: Dict[str, Any]):
        self.messages.append(message)
        if self.session:
            self.session.append(message)

    def load_history(self, messages: List[Dict[str, Any]]):
        self.messages = repair_history(messages)
        self.context_tokens = estimate_messages(self.messages)
        self.ctx.reset_state()  # checkpoints and reads belong to the previous conversation
        self.pinned = {}

    def clear(self):
        self.messages = []
        self.context_tokens = 0
        self.ctx.reset_state()

    def add_user_message(self, content: str, images: Optional[List[str]] = None):
        # Mistral rejects a user message directly after a tool result (turn cut short by an
        # interrupt, error or max_turns), so close the previous turn first.
        if self.messages and self.messages[-1].get("role") == "tool":
            self._append({"role": "assistant", "content": "(previous turn ended before a final answer)"})
        from hubble.images import user_content
        self._append({"role": "user", "content": user_content(content, images)})

    def context_ratio(self) -> float:
        window = int(self.settings.get("context_window", 128000)) or 1
        return self.context_tokens / window

    # ----- main loop -----------------------------------------------------

    def run(self, prompt: str, images: Optional[List[str]] = None) -> str:
        """images: data: URLs to send with the prompt (the model must support vision)."""
        started = time.time()
        saved = self._begin_route(prompt) if not self.is_subagent else None
        result = ""
        try:
            result = self._run(prompt, images)
            return result
        finally:
            s = self.last_stats
            ok = bool(result and result.strip()) and not s.error and not s.interrupted
            reason = "interrupted" if s.interrupted else ("error: " + s.error[:80] if s.error else
                                                           ("" if ok else "no answer"))
            route = self._route
            self.stats_log.task(self.provider_name, self.model, ok, reason=reason, calls=s.model_calls,
                                tools=s.tool_calls, prompt_tokens=s.prompt_tokens,
                                completion_tokens=s.completion_tokens, duration=time.time() - started,
                                tier=route["tier"] if route else "", escalated=bool(route and route["escalated"]),
                                subagent=self.is_subagent)
            if saved is not None:
                self.provider, self.provider_name, self.model = saved
            self._route = None

    # ----- routing ---------------------------------------------------------

    def _client(self, provider_name: str):
        if provider_name == self.provider_name:
            return self.provider
        return self.client_for(provider_name) if self.client_for else None

    def _switch(self, spec: str) -> bool:
        from hubble.router import parse_spec
        prov, model = parse_spec(spec, self.provider_name)
        client = self._client(prov)
        if client is None:
            self.events.notice(f"Routing: provider '{prov}' is not configured; staying on {self.model}.", "warn")
            return False
        self.provider, self.provider_name, self.model = client, prov, model
        return True

    def _begin_route(self, prompt: str):
        """Pick the tier for this prompt and switch to its model. Returns what to restore, or None."""
        from hubble.router import Struggle, classify, routing_config
        cfg = routing_config(self.settings)
        if not cfg:
            return None
        saved = (self.provider, self.provider_name, self.model)
        route = classify(prompt)
        if not self._switch(cfg[route.tier]):
            return None
        self._route = {"tier": route.tier, "cfg": cfg, "struggle": Struggle(), "escalated": False}
        self.last_route = (route.tier, self.provider_name, self.model, route.reasons)
        self.events.notice(f"→ {route.tier}: {self.model} ({'; '.join(route.reasons)})", "dim")
        return saved

    def _maybe_escalate(self) -> bool:
        """If the fast model is struggling, move the rest of this task to the strong model."""
        r = self._route
        if not r or r["tier"] != "fast" or r["escalated"]:
            return False
        why = r["struggle"].reason()
        if not why:
            return False
        if not self._switch(r["cfg"]["strong"]):
            return False
        r["escalated"] = True
        self.events.notice(f"Fast model struggling ({why}); switching to {self.model} for the rest of this task.",
                           "warn")
        return True

    def _run(self, prompt: str, images: Optional[List[str]] = None) -> str:
        stats = RunStats()
        self.last_stats = stats
        if not self.is_subagent:
            self.cancel.clear()
        hook = self.hooks.run("UserPromptSubmit", {"prompt": prompt})
        if hook.decision == "block":
            self.events.notice(f"Blocked by hook: {hook.reason}", "warn")
            stats.error = hook.reason
            return ""
        if hook.additional_context:
            prompt = f"{prompt}\n\n<hook-context>\n{hook.additional_context}\n</hook-context>"
        self.ctx.begin_turn()
        self.add_user_message(prompt, images)
        user_msg = self.messages[-1]
        try:
            return self._loop(stats)
        except KeyboardInterrupt:
            stats.interrupted = True
            self.events.turn_end(TurnResult())  # flush partially streamed text
            if self._partial_text:
                self._append({"role": "assistant", "content": self._partial_text + "\n[interrupted by user]"})
            self._close_dangling_tool_calls()
            self.events.notice("Interrupted.", "warn")
            return ""
        except ProviderError as e:
            stats.error = str(e)
            self.events.turn_end(TurnResult())
            if self._partial_text:
                self._append({"role": "assistant", "content": self._partial_text + "\n[response cut off by an API error]"})
            if self.messages and self.messages[-1] is user_msg:
                # Nothing happened yet; drop the prompt so a retry does not send it twice.
                self.messages.pop()
                if self.session:
                    self.session.reset(self.messages)
            self._close_dangling_tool_calls()
            self.events.notice(f"API error: {e}", "error")
            return ""
        finally:
            self._partial_text = ""

    _partial_text = ""

    def _on_text(self, delta: str):
        if self.cancel.is_set():
            raise KeyboardInterrupt
        self._partial_text += delta
        self.events.text(delta)

    def _loop(self, stats: RunStats) -> str:
        max_turns = int(self.settings.get("max_turns", 40))
        schemas = [t.schema() for t in self.tools]
        for turn in range(max_turns):
            if self.cancel.is_set():
                raise KeyboardInterrupt
            left = max_turns - turn
            if self.is_subagent and left == 3:
                self._note_on_last_tool_result(
                    "[Hubble: 3 steps left. Finish what you are doing and write your final report next; "
                    "do not start new searches.]")
            ratio = float(self.settings.get("auto_compact_ratio", 0.8))
            if ratio and self.context_ratio() >= ratio and len(self.messages) > 4:
                self.events.notice(f"Context {self.context_ratio():.0%} full; compacting...", "warn")
                self.compact(mid_turn=True)

            self._partial_text = ""
            self.events.turn_start()
            result = self._stream_with_fallback(schemas)
            self._partial_text = ""
            p_toks, c_toks = self._account(result, stats)
            served = self._served or (self.provider_name, self.model, False)
            self.stats_log.call(served[0], served[1], True, prompt_tokens=p_toks, completion_tokens=c_toks,
                                duration=result.duration, ttft_ms=result.ttft_ms, subagent=self.is_subagent,
                                fallback=served[2])
            self.events.turn_end(result)
            if self._route:
                self._route["struggle"].note_turn(result.text, len(result.tool_calls))
                if not result.text and not result.tool_calls and self._maybe_escalate():
                    continue  # retry this turn on the strong model instead of ending in silence

            text = result.text
            if not text and not result.tool_calls:
                text = "(empty response)"  # some backends reject empty assistant messages
            message: Dict[str, Any] = {"role": "assistant", "content": text}
            if result.raw_content:
                message["_anthropic_content"] = result.raw_content
            if result.tool_calls:
                message["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": c.arguments}}
                                         for c in result.tool_calls]
            self._append(message)

            if not result.tool_calls:
                if result.finish_reason == "length":
                    self.events.notice("Response hit max_tokens and was cut off. Say 'continue' or raise "
                                       "max_tokens in settings.", "warn")
                elif not result.text:
                    # The model produced neither text nor a tool call. Some backends do this after a long
                    # tool-call chain, or when a safety filter drops the reply; either way, say so instead
                    # of ending the turn in silence.
                    reason = f" (finish_reason={result.finish_reason})" if result.finish_reason else ""
                    self.events.notice(f"{self.model} returned an empty response{reason}. Try again, ask "
                                       "differently, or switch model with /model.", "warn")
                    return result.text
                if result.finish_reason != "length":
                    stop = self.hooks.run("Stop", {"final_text": result.text})
                    if stop.decision == "block":
                        self.events.notice(f"Hook says keep going: {stop.reason}", "dim")
                        self._append({"role": "user", "content": stop.reason})
                        continue
                return result.text

            if self._parallel_ok(result.tool_calls):
                outputs = self._execute_parallel(result.tool_calls)
            else:
                outputs = (self._execute(call) for call in result.tool_calls)
            for call, output in zip(result.tool_calls, outputs):
                stats.tool_calls += 1
                self._append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output})
                self.context_tokens += estimate_tokens(output)
                if self._route:
                    failed = output.startswith(("Error:", "Permission denied", "Blocked by hook", "The user denied"))
                    self._route["struggle"].note_tool(output, failed)
            self._maybe_escalate()

        stats.hit_step_limit = True
        if self.is_subagent:
            return self._final_report(schemas, stats)
        self.events.notice(f"Stopped after {max_turns} model calls (max_turns). Say 'continue' to keep going.", "warn")
        return ""

    def _note_on_last_tool_result(self, note: str):
        """Tell the model something mid-task without adding a user turn right after a tool result
        (which some backends, e.g. Mistral, reject): the note rides on the last tool result."""
        for m in reversed(self.messages):
            if m.get("role") == "tool":
                m["content"] = f"{m.get('content') or ''}\n\n{note}"
                return
            if m.get("role") != "assistant":
                break

    def _final_report(self, schemas, stats: RunStats) -> str:
        """A sub-agent out of steps still owes a report: ask for one last answer from what it has
        found, instead of returning nothing and throwing all of its reading away."""
        self._note_on_last_tool_result(
            "[Hubble: step limit reached. Do NOT call any more tools. Write your final report now from "
            "what you have found so far, and list what you could not check.]")
        try:
            result = self._stream_with_fallback(schemas)
            self._account(result, stats)
            self.events.turn_end(result)
        except ProviderError:
            result = TurnResult()
        if result.text.strip():
            self._append({"role": "assistant", "content": result.text})
            return result.text + "\n\n(Note: this sub-agent ran out of steps; the report may be incomplete.)"
        # The model still would not write one: hand back what it looked at, so nothing is lost.
        looked = []
        for m in self.messages:
            for tc in m.get("tool_calls") or []:
                try:
                    a = parse_arguments(tc["function"]["arguments"])
                except ValueError:
                    a = {}
                what = a.get("path") or a.get("pattern") or a.get("query") or a.get("command") or ""
                looked.append(f"{tc['function']['name']}({what})")
        return ("(This sub-agent ran out of steps before writing a report. It looked at: "
                + ", ".join(looked[-40:]) + ". Read the most relevant of these yourself instead of "
                "re-running the same task.)")

    def _stream_with_fallback(self, schemas) -> TurnResult:
        messages = [{"role": "system", "content": self.system_prompt()}] + self.messages
        kwargs = dict(tools=schemas, temperature=self.temperature,
                      max_tokens=int(self.settings.get("max_tokens", 8192)),
                      on_text=self._on_text, on_reasoning=self.events.reasoning)
        self._served = (self.provider_name, self.model, False)
        started = time.time()
        try:
            return self.provider.stream(self.model, messages, **kwargs)
        except ProviderError as e:
            self.stats_log.call(self.provider_name, self.model, False, status=e.status, error=str(e),
                                duration=time.time() - started, subagent=self.is_subagent)
            if not e.transient or self._partial_text:
                raise
            if self.fallback_resolver is not None:
                target = self.fallback_resolver(self.provider_name, self.model)
            else:
                fb = self.settings.get("fallback_model")
                target = (self.fallback_client, fb) if fb and self.fallback_client else None
            if not target or target == (self.provider, self.model):
                raise
            client, fallback = target
            where = "" if client is self.provider else " on another provider"
            self.events.notice(f"{self.model} failed ({e}). Using fallback model {fallback}{where} for this "
                               "request; /model to switch, /fallback to choose the fallback.", "warn")
            fb_provider = self.provider_name if client is self.provider else getattr(client, "hubble_name",
                                                                                      "fallback")
            self._served = (fb_provider, fallback, True)
            started = time.time()
            try:
                return client.stream(fallback, messages, **kwargs)
            except ProviderError as e2:
                self.stats_log.call(fb_provider, fallback, False, status=e2.status, error=str(e2),
                                    duration=time.time() - started, subagent=self.is_subagent, fallback=True)
                raise

    def _parallel_ok(self, calls: List[ToolCall]) -> bool:
        """Several sub-agent tasks in one turn run concurrently, as long as none of them can
        edit -- an edit-capable sub-agent may need to ask the user something mid-run, and
        overlapping approval prompts on one terminal is not something to risk."""
        tools = self.tools_by_name
        if not (len(calls) > 1 and any(c.name == "task" for c in calls)
                and all(c.name in tools and tools[c.name].kind == "read" for c in calls)):
            return False
        for c in calls:
            if c.name != "task":
                continue
            try:
                a = parse_arguments(c.arguments)
            except ValueError:
                continue
            spec = next((d for d in self.agent_defs if d.name == str(a.get("agent", "")).lower()), None)
            if a.get("capability") == "edit" or (spec and spec.capability == "edit"):
                return False
        return True

    def _execute_parallel(self, calls: List[ToolCall]) -> List[str]:
        self.events.batch_start(len(calls))
        pool = ThreadPoolExecutor(max_workers=min(4, len(calls)), thread_name_prefix="hubble-task")
        futures = [pool.submit(self._execute, c) for c in calls]
        try:
            # Poll so Ctrl+C reaches the main thread on Windows.
            while not all(f.done() for f in futures):
                wait(futures, timeout=0.2)
            outputs = []
            for f in futures:
                try:
                    outputs.append(f.result())
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # a crash in one task must not lose the others' reports
                    outputs.append(f"Error: task crashed: {type(e).__name__}: {e}")
            return outputs
        except KeyboardInterrupt:
            self.cancel.set()
            for f in futures:
                f.cancel()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            self.events.batch_end()

    def _account(self, result: TurnResult, stats: RunStats):
        prompt_toks = result.usage.get("prompt_tokens")
        completion_toks = result.usage.get("completion_tokens")
        if prompt_toks is None:
            prompt_toks = estimate_messages(self.messages) + estimate_tokens(self.system_prompt())
        if completion_toks is None:
            completion_toks = estimate_tokens(result.text + result.reasoning) + sum(
                estimate_tokens(c.arguments) for c in result.tool_calls)
        stats.prompt_tokens += prompt_toks
        stats.completion_tokens += completion_toks
        stats.duration += result.duration
        stats.model_calls += 1
        self.total_prompt_tokens += prompt_toks
        self.total_completion_tokens += completion_toks
        self.context_tokens = prompt_toks + completion_toks
        return prompt_toks, completion_toks

    def _close_dangling_tool_calls(self):
        repaired = repair_history(self.messages)
        if len(repaired) != len(self.messages):
            self.messages = repaired
            if self.session:
                self.session.reset(self.messages)

    def _execute(self, call: ToolCall) -> str:
        tool = self.tools_by_name.get(call.name)
        if tool is None:
            return f"Error: unknown tool '{call.name}'. Available tools: {', '.join(self.tools_by_name)}"
        try:
            args = parse_arguments(call.arguments)
            tool.validate(args)
        except (ValueError, ToolError) as e:
            output = f"Error: {e}"
            self.events.tool_result(tool, {}, output, True)
            return output

        pre = self.hooks.run("PreToolUse", {"tool": tool.name, "args": args}, name=tool.name)
        if pre.decision == "block":
            output = f"Blocked by hook: {pre.reason}"
            self.events.tool_result(tool, args, output, True)
            return output

        target = tool.target(args)
        if tool.kind != "exec" and tool.name not in ("task", "todo_write") and target:
            # Match rules against the canonical workspace-relative path, not the raw argument,
            # so ./secrets/x, absolute paths and a/../secrets/x hit the same rule.
            try:
                target = self.ctx.rel(self.ctx.resolve(target))
            except ToolError:
                pass
        decision, reason = self.permissions.check(tool.name, tool.kind, target)
        if (decision == "allow" and tool.name == "shell" and args.get("unsandboxed")
                and self.permissions.mode != "yolo"):
            from hubble.sandbox import effective_mode
            if effective_mode(self.ctx.sandbox) != "off":
                decision = "ask"  # leaving the sandbox always needs a human, whatever allow rules say
        if decision == "deny":
            output = f"Permission denied: {reason}."
            self.events.tool_result(tool, args, output, True)
            return output
        if decision == "ask":
            try:
                tool.precheck(args, self.ctx)
            except (ToolError, OSError) as e:
                output = f"Error: {e}"
                self.events.tool_result(tool, args, output, True)
                return output
            try:
                preview = tool.preview(args, self.ctx)
            except (ToolError, OSError) as e:
                preview = f"(preview unavailable: {e})"
            self.hooks.run("Notification", {"message": f"Hubble needs your permission to use {tool.name}",
                                            "tool": tool.name, "target": target}, name=tool.name)
            answer, feedback = self.events.ask(tool, args, preview)
            if answer == "always":
                rule = self.permissions.always_rule(tool.name, tool.kind, target)
                self.events.notice(f"Allowed for this session: {rule}")
            elif answer != "yes":
                output = "The user denied this tool call."
                if feedback:
                    output += f" User feedback: {feedback}"
                self.events.tool_result(tool, args, output, True)
                return output

        self.events.tool_start(tool, args)
        output, is_error = run_tool(tool, args, self.ctx)
        post = self.hooks.run("PostToolUse", {"tool": tool.name, "args": args, "output": output,
                                              "is_error": is_error}, name=tool.name)
        if post.additional_context:
            output += f"\n\n<hook-context>\n{post.additional_context}\n</hook-context>"
        self.events.tool_result(tool, args, output, is_error)
        if tool.name == "todo_write" and not is_error:
            self.events.todos(self.ctx.todos)
        elif tool.name == "write_skill" and not is_error:
            self.reload_skills()
        return output

    # ----- context management -------------------------------------------

    def compact(self, focus: str = "", mid_turn: bool = False) -> bool:
        if len(self.messages) < 3:
            return False
        pre = self.hooks.run("PreCompact", {"trigger": "auto" if mid_turn else "manual", "focus": focus})
        if pre.decision == "block":
            self.events.notice(f"Compaction blocked by hook: {pre.reason}", "warn")
            return False
        lines = []
        for m in self.messages:
            role = m.get("role")
            if role == "tool":
                lines.append(f"[tool result {m.get('name', '')}]\n{truncate(m.get('content') or '', 1500)}")
            else:
                from hubble.images import content_text
                text = content_text(m.get("content") or "")
                for tc in m.get("tool_calls") or []:
                    text += f"\n[calls {tc['function']['name']}({truncate(tc['function']['arguments'], 400)})]"
                lines.append(f"[{role}]\n{text}")
        budget = int(int(self.settings.get("context_window", 128000)) * 3.8 * 0.6)
        transcript = truncate("\n\n".join(lines), budget)
        instruction = COMPACT_PROMPT + (f"\nFocus especially on: {focus}" if focus else "")
        summary = self.provider.complete(
            self.model,
            [{"role": "system", "content": "You write precise summaries of coding sessions."},
             {"role": "user", "content": f"<transcript>\n{transcript}\n</transcript>\n\n{instruction}"}],
            max_tokens=2500)
        if not summary.strip():
            self.events.notice("Compaction returned an empty summary; history kept.", "warn")
            return False
        head = {"role": "user", "content": f"<conversation-summary>\n{summary.strip()}\n</conversation-summary>\n"
                                           "The earlier conversation was compacted into the summary above."
                                           + (" Continue the current task from where it left off." if mid_turn else "")}
        self.messages = [head] if mid_turn else [head, {"role": "assistant", "content": "Understood. I have the context."}]
        if self.session:
            self.session.reset(self.messages)
        self.context_tokens = estimate_messages(self.messages)
        self.events.notice(f"Compacted history into a {estimate_tokens(summary)}-token summary.")
        return True


SUBAGENT_VERBS = {"read_file": "reading", "grep": "searching for", "glob": "finding", "list_dir": "listing",
                  "web_search": "searching the web for"}


class _SubagentEvents(Events):
    """Forwards a sub-agent's progress to the parent UI under a stable key."""

    def __init__(self, parent: Events, label: str, key: str):
        self.parent = parent
        self.label = label
        self.key = key

    def turn_start(self):
        self.parent.subagent_step(self.key, "thinking", is_tool=False)

    def tool_start(self, tool, args):
        verb = SUBAGENT_VERBS.get(tool.name, tool.name)
        self.parent.subagent_step(self.key, f"{verb} {describe_call(tool, args)}".strip())

    def turn_end(self, result):
        used = result.usage.get("total_tokens") or (
            result.usage.get("prompt_tokens", 0) + result.usage.get("completion_tokens", 0))
        if not used:
            used = estimate_tokens(result.text + result.reasoning)
        self.parent.subagent_tokens(self.key, used)

    def notice(self, message, level="info"):
        if level in ("warn", "error"):
            self.parent.notice(f"  ↳ {self.label}: {message}", level)

    def ask(self, tool, args, preview):
        # An edit-capable sub-agent's edits/commands still ask the real user, through the same
        # UI -- edit-batches are never parallelized (see _parallel_ok), so this is always safe
        # to call from the main thread, never from a background worker.
        self.parent.notice(f"  ↳ {self.label} wants to:", "dim")
        return self.parent.ask(tool, args, preview)


_task_ids = itertools.count(1)


class TaskTool(Tool):
    name = "task"
    BASE_DESCRIPTION = ("Launch a sub-agent for a self-contained piece of work. Two capabilities:\n"
                   "- read_only (default): research that needs many searches or file reads (e.g. 'find "
                   "where auth tokens are validated and explain the flow'). Returns a written report. "
                   "Cannot edit files or run commands. Several read_only tasks in one turn run in parallel.\n"
                   "- edit: a self-contained implementation task with its own file/shell tools (e.g. "
                   "'add input validation to the signup form in src/signup.py'). Its edits and commands "
                   "still go through the same approval you would see if you did them yourself. Give it "
                   "everything it needs to work without asking you follow-up questions -- it cannot ask.\n"
                   "Use this to parallelize independent research, or to delegate one well-scoped change "
                   "while you keep working on something else in the same turn.")
    kind = "read"

    def __init__(self, parent: Agent):
        self.parent = parent

    def _defs(self):
        return {a.name: a for a in (getattr(self.parent, "agent_defs", None) or [])}

    @property
    def parameters(self):
        props = {
            "description": {"type": "string", "description": "3-5 word task label"},
            "prompt": {"type": "string", "description": "Detailed, self-contained instructions for the sub-agent"},
            "capability": {"type": "string", "enum": ["read_only", "edit"],
                           "description": "read_only (default) or edit"},
            "model": {"type": "string", "description": "Model for this sub-agent, 'model' or 'provider:model' "
                                                          "(default: same as you). Pick from the team below."},
            "isolation": {"type": "string", "enum": ["none", "worktree"],
                          "description": "edit only. worktree: work in a fresh git worktree on its own branch, "
                                         "so the main checkout is untouched until you review and merge"},
        }
        defs = self._defs()
        if defs:
            props["agent"] = {"type": "string", "enum": sorted(defs),
                              "description": "Use a named specialist agent (its own prompt, tools and model)"}
        return {"type": "object", "properties": props, "required": ["description", "prompt"]}

    @property
    def description(self):
        text = self.BASE_DESCRIPTION
        defs = self._defs()
        if defs:
            lines = "\n".join(f"- {a.name}: {a.description}" for a in defs.values())
            text += ("\nNamed specialist agents (pass `agent`; their capability, tools and model come from "
                     "their definition):\n" + lines)
        team = team_block(getattr(self.parent, "settings", None) or {})
        return text + ("\n" + team if team else "")

    def target(self, args):
        return args.get("description", "")

    def run(self, args, ctx):
        p = self.parent
        spec = None
        if args.get("agent"):
            spec = self._defs().get(str(args["agent"]).lower())
            if spec is None:
                raise ToolError(f"no agent named '{args['agent']}'. Available: {', '.join(self._defs()) or '(none)'}")
        edit = (spec.capability if spec else args.get("capability")) == "edit"
        isolated = None
        work_root = ctx.root
        if edit and args.get("isolation") == "worktree":
            from hubble import worktree
            slug = re.sub(r"[^a-z0-9]+", "-", (args.get("description") or "task").lower()).strip("-")[:30] or "task"
            try:
                isolated = worktree.create(ctx.root, f"task-{int(time.time()) % 100000}-{slug}")
            except worktree.WorktreeError as e:
                raise ToolError(f"could not create an isolated worktree: {e}")
            work_root = Path(isolated["path"])
        sub_ctx = ToolContext(root=work_root, extra_dirs=ctx.extra_dirs, allow_secrets=ctx.allow_secrets,
                              shell_argv=ctx.shell_argv, shell_timeout=ctx.shell_timeout,
                              sandbox=ctx.sandbox, sandbox_image=ctx.sandbox_image,
                              sandbox_memory=ctx.sandbox_memory, sandbox_cpus=ctx.sandbox_cpus,
                              sandbox_network=ctx.sandbox_network, sandbox_writable=ctx.sandbox_writable)
        if edit:
            # Full toolset except task/write_skill: an edit sub-agent does its own assigned job,
            # it does not spawn further sub-agents or rewrite the project's skills.
            tools = [t for t in default_tools() + web_tools(p.settings)
                    if t.name not in ("task", "write_skill")]
            perms = p.permissions  # same mode and rules as the parent: edits/commands ask the same way
            if isolated and perms.mode == "default":
                # File edits land in a throwaway checkout, so they need no approval; shell commands
                # still ask, since they can reach outside it.
                perms = Permissions("accept-edits", allow=perms.allow, deny=perms.deny)
            system = ("You are an implementation sub-agent inside Hubble, working on one well-scoped task "
                      f"delegated to you in the workspace at {work_root}. You have read_file, write_file, "
                      "edit_file, shell, grep, glob, list_dir and todo_write. You cannot ask the user "
                      "anything -- if the task is ambiguous, make the most reasonable choice and say what "
                      "you assumed in your final report. Verify your change (run tests/a build) before "
                      "finishing. Finish with a concise report of what you changed and how you verified it.")
        else:
            # Read-only tools plus web search; web_fetch needs per-domain approval, which sub-agents can't ask for.
            tools = [t for t in default_tools() + web_tools(p.settings)
                    if t.name in READ_ONLY_TOOL_NAMES | {"web_search"}]
            perms = Permissions("plan", deny=p.permissions.deny)
            system = ("You are a read-only research sub-agent inside Hubble. Use the tools to investigate "
                      f"the workspace at {ctx.root} and answer the task. Finish with a concise, factual "
                      "report citing path:line. You cannot edit files or run commands.")
        if spec:
            if spec.tools:
                # A definition narrows the capability's toolset; it never widens it (a read_only
                # agent listing write_file still does not get it).
                tools = [t for t in tools if t.name in spec.tools]
            tool_names = ", ".join(t.name for t in tools) or "(none)"
            system = (f"{spec.prompt}\n\n---\nYou are the '{spec.name}' sub-agent inside Hubble, working in "
                      f"{ctx.root}. Your tools: {tool_names}. You cannot ask the user anything; make "
                      "reasonable assumptions and state them. End with a concise final report.")
        label = args.get("description") or (spec.name if spec else "task")
        if spec:
            label = f"{spec.name}: {label}"
        # Models to try, in order: the one this call asks for, else the agent definition's, else a
        # team member that suits the job, else (research) the fast routing tier, else the parent's
        # own. Then, if that model fails outright, the other team members. "provider:model" works.
        from hubble.router import parse_spec, routing_config
        choice = args.get("model") or (spec.model if spec else "") or pick_team_model(p.settings, edit)
        if not choice and not edit:
            cfg = routing_config(p.settings)
            choice = cfg["fast"] if cfg else ""
        candidates = [parse_spec(choice, p.provider_name) if choice else (p.provider_name, p.model)]
        for alt in team_specs(p.settings):
            pm = parse_spec(alt, p.provider_name)
            if pm not in candidates:
                candidates.append(pm)
        if (p.provider_name, p.model) not in candidates:
            candidates.append((p.provider_name, p.model))
        candidates = candidates[:3]  # the first choice plus at most two retries

        key = f"task-{next(_task_ids)}"
        base_label = label
        first = candidates[0]
        if first != (p.provider_name, p.model):
            label = f"{base_label} · {first[1]}"
        p.events.subagent_start(key, label)
        status, detail = "failed", "crashed"
        subs: List[Agent] = []
        try:
            report = None
            for attempt, (prov, model) in enumerate(candidates):
                client = p.provider if prov == p.provider_name else (p.client_for(prov) if p.client_for else None)
                if client is None:
                    p.events.notice(f"  ↳ {base_label}: provider '{prov}' is not configured; skipping {model}", "warn")
                    continue
                if attempt:
                    p.events.subagent_step(key, f"retrying on {model}", is_tool=False)
                steps = int(p.settings.get("subagent_max_turns") or (40 if edit else 30))
                sub = Agent(client, {**p.settings, "model": model, "max_turns": steps,
                                     "auto_compact_ratio": 0},
                            sub_ctx, perms, _SubagentEvents(p.events, label, key), tools=tools,
                            system_override=f"{system}\n\nYou have at most {steps} steps (model calls). Use "
                                            "them well: call several tools in one step when they are "
                                            "independent (e.g. read three files at once), and write your "
                                            "report as soon as you can answer; do not read everything.")
                subs.append(sub)
                sub.cancel = p.cancel
                sub.is_subagent = True
                sub.provider_name = prov
                sub.fallback_client = p.fallback_client
                sub.fallback_resolver = p.fallback_resolver
                sub.stats_log = p.stats_log
                sub.client_for = p.client_for
                report = sub.run(args["prompt"])
                if sub.last_stats.interrupted:
                    status, detail = "stopped", "stopped by user"
                    raise KeyboardInterrupt  # Ctrl+C must stop the parent turn too
                if not sub.last_stats.error:
                    if attempt:
                        report = f"(ran on {prov}:{model} after the first model failed)\n{report or ''}"
                    break
                # The model itself failed (API error). Retry on the next model, but only when that
                # cannot redo side effects: research tasks, or edit tasks that have not acted yet.
                detail = f"failed: {sub.last_stats.error}"
                if edit and sub.last_stats.tool_calls:
                    raise ToolError(f"sub-agent failed after making changes: {sub.last_stats.error}")
                if attempt < len(candidates) - 1:
                    p.events.notice(f"  ↳ {base_label}: {model} failed ({sub.last_stats.error[:80]}); "
                                    "trying the next model", "warn")
            else:
                raise ToolError(f"sub-agent failed on every model tried: {detail.removeprefix('failed: ')}")
            limited = subs[-1].last_stats.hit_step_limit if subs else False
            if not (report or "").strip():
                status, detail = "failed", "no report"
            elif limited:
                status, detail = "partial", f"ran out of steps · partial report ({len(report):,} chars)"
            else:
                status, detail = "done", f"report ready ({len(report):,} chars)"
            report = report or "(sub-agent returned no report)"
            if isolated:
                from hubble import worktree
                rel = ctx.rel(work_root)
                branch = isolated["branch"]
                sha = worktree.commit_all(work_root, f"hubble task: {label}")
                if sha:
                    stat = worktree._git(work_root, "show", "--stat", "--format=", "HEAD", check=False)
                    report += (f"\n\n<worktree path=\"{rel}\" branch=\"{branch}\" commit=\"{sha}\">\n{stat}\n</worktree>\n"
                               f"These changes are committed on branch {branch} only; the main checkout is "
                               f"untouched. Review with `git show {sha}`; if they are right, bring them in with "
                               f"`git cherry-pick {sha}` (or `git merge {branch}`), or tell the user they can.")
                else:
                    st = worktree.status(work_root)
                    report += (f"\n\n<worktree path=\"{rel}\" branch=\"{branch}\">\n"
                               f"{st['dirty'] or 'no changes'}\n</worktree>\n"
                               + ("The changes are uncommitted and only in that worktree." if st["dirty"] else
                                  "The sub-agent made no file changes."))
            stop = p.hooks.run("SubagentStop", {"description": label, "capability": "edit" if edit else "read_only",
                                                "agent": args.get("agent"), "report": report}, name=label)
            if stop.additional_context:
                report += f"\n\n<hook-context>\n{stop.additional_context}\n</hook-context>"
            return report
        finally:
            for sub in subs:
                p.total_prompt_tokens += sub.total_prompt_tokens
                p.total_completion_tokens += sub.total_completion_tokens
            p.events.subagent_end(key, status, detail)


def describe_call(tool: Tool, args: Dict[str, Any]) -> str:
    """One-line human label for a tool call."""
    if tool.name == "shell":
        return args.get("command", "")
    if tool.name in ("grep", "glob"):
        where = args.get("path")
        return f"{args.get('pattern', '')}" + (f" in {where}" if where else "")
    if tool.name == "task":
        return args.get("description", "")
    if tool.name == "web_search":
        return args.get("query", "")
    if tool.name == "web_fetch":
        return args.get("url", "")
    if tool.name == "todo_write":
        return f"{len(args.get('todos') or [])} items"
    if tool.name == "read_file" and (args.get("offset") or args.get("limit")):
        return f"{args.get('path')} (from line {args.get('offset') or 1})"
    target = tool.target(args)
    return target or json.dumps(args)[:80]


_TEAM_CACHE: Dict[str, Any] = {"at": 0.0, "key": None, "text": ""}


def team_block(settings: Dict[str, Any]) -> str:
    """The user's sub-agent model team for the task tool description: each model, what it is for,
    and how it has actually done (from /stats), so the main model can match model to job."""
    team = settings.get("subagent_models") or []
    if not team:
        return ""
    key = json.dumps(team, sort_keys=True)
    if _TEAM_CACHE["key"] == key and time.time() - _TEAM_CACHE["at"] < 60:
        return _TEAM_CACHE["text"]
    from hubble import stats
    try:
        rows = {(r["provider"], r["model"]): r for r in stats.summarize(stats.load(days=30))}
    except Exception:
        rows = {}
    lines = []
    for entry in team:
        spec, use = (entry.get("model", ""), entry.get("use", "")) if isinstance(entry, dict) else (str(entry), "")
        if not spec:
            continue
        from hubble.router import parse_spec
        prov, model = parse_spec(spec, settings.get("provider") or "")
        r = rows.get((prov, model))
        perf = ""
        if r and r["calls"]:
            bits = [f"{r['call_success']:.0%} of {r['calls']} calls ok"]
            if r["tok_per_s"]:
                bits.append(f"{r['tok_per_s']:.0f} tok/s")
            perf = f" [{', '.join(bits)}]"
        lines.append(f"- {spec}" + (f": {use}" if use else "") + perf)
    text = ("Model team for sub-agents (set `model` to one of these; match the model to the job, e.g. a fast "
            "one for broad searching and a strong one for tricky changes or review; for a multi-part goal, "
            "give each task the model that suits its part):\n" + "\n".join(lines)) if lines else ""
    _TEAM_CACHE.update(at=time.time(), key=key, text=text)
    return text


def team_specs(settings: Dict[str, Any]) -> List[str]:
    return [(e.get("model", "") if isinstance(e, dict) else str(e)) for e in (settings.get("subagent_models") or [])
            if (e.get("model") if isinstance(e, dict) else e)]


_FAST_USE = re.compile(r"fast|quick|cheap|search|read|explor|look|research|summar", re.I)
_STRONG_USE = re.compile(r"strong|careful|smart|review|edit|change|fix|implement|refactor|code", re.I)


def pick_team_model(settings: Dict[str, Any], edit: bool) -> str:
    """When the main model did not choose, pick the team member whose note fits the job:
    a fast/search one for research, a careful/review one for edits. '' if nothing fits."""
    team = [e for e in (settings.get("subagent_models") or []) if isinstance(e, dict) and e.get("model")]
    want = _STRONG_USE if edit else _FAST_USE
    match = next((e["model"] for e in team if want.search(e.get("use", ""))), "")
    if match or edit:
        return match  # edits without a matching member stay on the main model
    return team_specs(settings)[0] if team_specs(settings) else ""
