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
    error: Optional[str] = None


class Agent:
    def __init__(self, provider: OpenAICompatProvider, settings: Dict[str, Any], ctx: ToolContext,
                 permissions: Permissions, events: Events, session: Optional[Session] = None,
                 tools: Optional[List[Tool]] = None, system_override: Optional[str] = None):
        self.provider = provider
        self.provider_name: str = settings.get("provider") or "hubble"
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
        if self.ctx.sandbox == "docker":
            shell_label = (f"an isolated Docker container (image {self.ctx.sandbox_image}), reached via "
                          f"`sh -lc`; use POSIX/Linux syntax regardless of the host OS. Only /workspace "
                          f"(this project) is visible inside it"
                          + ("" if self.ctx.sandbox_network else "; it has no network access"))
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
        for _ in range(max_turns):
            if self.cancel.is_set():
                raise KeyboardInterrupt
            ratio = float(self.settings.get("auto_compact_ratio", 0.8))
            if ratio and self.context_ratio() >= ratio and len(self.messages) > 4:
                self.events.notice(f"Context {self.context_ratio():.0%} full; compacting...", "warn")
                self.compact(mid_turn=True)

            self._partial_text = ""
            self.events.turn_start()
            result = self._stream_with_fallback(schemas)
            self._partial_text = ""
            self._account(result, stats)
            self.events.turn_end(result)

            text = result.text
            if not text and not result.tool_calls:
                text = "(empty response)"  # some backends reject empty assistant messages
            message: Dict[str, Any] = {"role": "assistant", "content": text}
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

        self.events.notice(f"Stopped after {max_turns} model calls (max_turns). Say 'continue' to keep going.", "warn")
        return ""

    def _stream_with_fallback(self, schemas) -> TurnResult:
        messages = [{"role": "system", "content": self.system_prompt()}] + self.messages
        kwargs = dict(tools=schemas, temperature=self.temperature,
                      max_tokens=int(self.settings.get("max_tokens", 8192)),
                      on_text=self._on_text, on_reasoning=self.events.reasoning)
        try:
            return self.provider.stream(self.model, messages, **kwargs)
        except ProviderError as e:
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
            return client.stream(fallback, messages, **kwargs)

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
            "model": {"type": "string", "description": "Model for this sub-agent (default: same as you)"},
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
        defs = self._defs()
        if not defs:
            return self.BASE_DESCRIPTION
        lines = "\n".join(f"- {a.name}: {a.description}" for a in defs.values())
        return (self.BASE_DESCRIPTION + "\nNamed specialist agents (pass `agent`; their capability, tools "
                "and model come from their definition):\n" + lines)

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
                              sandbox_network=ctx.sandbox_network)
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
        key = f"task-{next(_task_ids)}"
        p.events.subagent_start(key, label)
        model = args.get("model") or (spec.model if spec else "") or p.model
        sub = Agent(p.provider, {**p.settings, "model": model, "max_turns": 30 if edit else 20,
                                "auto_compact_ratio": 0},
                    sub_ctx, perms, _SubagentEvents(p.events, label, key), tools=tools, system_override=system)
        sub.cancel = p.cancel
        sub.is_subagent = True
        # Same provider and fallback route as the parent (it may have switched provider mid-session).
        sub.provider_name = p.provider_name
        sub.fallback_client = p.fallback_client
        sub.fallback_resolver = p.fallback_resolver
        status, detail = "failed", "crashed"
        try:
            report = sub.run(args["prompt"])
            if sub.last_stats.interrupted:
                status, detail = "stopped", "stopped by user"
                raise KeyboardInterrupt  # Ctrl+C must stop the parent turn too
            if sub.last_stats.error:
                detail = f"failed: {sub.last_stats.error}"
                raise ToolError(f"sub-agent failed: {sub.last_stats.error}")
            status = "done"
            detail = f"report ready ({len(report or ''):,} chars)" if report else "no report"
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
