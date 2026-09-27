#!/usr/bin/env python3
"""
Hubble Legacy CLI (v3.0, kept for --legacy)
Modeled after Claude Code and Google Antigravity CLI (`agy`).

Features:
- Full Autonomous Agent & Tool Loop:
  * File reading with line numbers (<tool:read path="..."/>)
  * Direct file writing/creation (<tool:write path="...">...</tool:write>)
  * Surgical search-and-replace patching (<tool:edit path="...">...</tool:edit>)
  * Terminal command execution (<tool:run>...</tool:run>)
  * Workspace exploration (<tool:list>, <tool:find>, <tool:grep>)
  * Interactive approval mode with instant auto-run toggle (/auto)
- Developer Shortcuts:
  * Direct shell command execution (!<cmd> or /run <cmd>)
  * In-memory workspace file loading (/add, /drop, /files)
  * Categorized model registry (/models) with fuzzy switcher (/model)
  * Default coding model: `codestral-latest` (Mistral 22B Coding Model)
  * Multiple developer modes (/mode code, debug, review, architect, chat)
  * Multi-line paste mode (<<< ... >>> or \"\"\" ... \"\"\")
- Token Performance Analytics:
  * Per-turn input (prompt) tokens, output (completion) tokens, TTFT, and tok/s speed
  * Cumulative session token counter and history compaction (/compact)
- Windows ANSI color support & UTF-8 console safety
"""

import os
import sys
import time
import json
import re
import fnmatch
import subprocess
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import httpx

# Ensure UTF-8 console encoding on Windows to prevent charmap crashes
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import (
    BASE_URL,
    API_KEY,
    ensure_api_key,
    DEFAULT_MODEL,
    MODEL_CATEGORIES,
    RELIABLE_MODELS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT
)

DEFAULT_CACHE_FILE = str(Path(__file__).resolve().parent / "available_models.json")


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # Foreground colors
    BLACK = "\033[30m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    # Background colors
    BG_CYAN = "\033[46m"
    BG_MAGENTA = "\033[45m"
    BG_DARK = "\033[100m"


def c(text: str, style_code: str) -> str:
    """Format text with ANSI style code."""
    return f"{style_code}{text}{Style.RESET}"


def load_cached_models(json_file: str = DEFAULT_CACHE_FILE) -> List[Dict[str, Any]]:
    """Load verified working models from JSON if available."""
    if os.path.exists(json_file):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("working_models", [])
        except Exception:
            pass
    return []


def estimate_tokens(text: str) -> int:
    """Accurate token estimator based on word and character heuristics."""
    if not text:
        return 0
    words = len(text.split())
    chars = len(text)
    return max(words, int(chars / 3.8 + 0.5))


def estimate_prompt_tokens(messages: List[Dict[str, str]]) -> int:
    """Estimate input tokens for chat messages payload."""
    total = 3
    for m in messages:
        total += 3
        total += estimate_tokens(m.get("content", ""))
    return total


# System Prompts for Personas with Tool Use Instructions
SYSTEM_PROMPTS = {
    "code": (
        "You are an expert autonomous AI software engineer and pair programmer (inspired by Claude Code and Google Antigravity CLI).\n"
        "You have direct access to tools to inspect, modify, and test code in the user's workspace.\n\n"
        "AVAILABLE TOOLS (output them in your response when needed):\n"
        "1. Read a file with line numbers:\n"
        "   <tool:read path=\"relative/path.py\" [offset=\"1\"] [limit=\"120\"] />\n\n"
        "2. Create or rewrite a file completely:\n"
        "   <tool:write path=\"relative/path.py\">\n"
        "   file contents here\n"
        "   </tool:write>\n\n"
        "3. Surgically patch an existing file:\n"
        "   <tool:edit path=\"relative/path.py\">\n"
        "   <<<<<<< SEARCH\n"
        "   exact lines to replace\n"
        "   =======\n"
        "   new replacement lines\n"
        "   >>>>>>>\n"
        "   </tool:edit>\n\n"
        "4. Run a terminal shell command:\n"
        "   <tool:run>command to execute</tool:run>\n\n"
        "5. List files in a directory:\n"
        "   <tool:list path=\"relative/dir\" />\n\n"
        "6. Find files by glob pattern:\n"
        "   <tool:find pattern=\"*.py\" />\n\n"
        "7. Grep for string in directory:\n"
        "   <tool:grep query=\"search_term\" [path=\".\"] />\n\n"
        "RULES FOR CODING & ACTIONS:\n"
        "- When asked to edit, fix, or implement features, inspect existing files first using <tool:read>.\n"
        "- Use <tool:edit> for surgical changes or <tool:write> for new files.\n"
        "- Always verify your work by running commands (tests, syntax checks) using <tool:run>.\n"
        "- Be concise, direct, and pragmatic. Avoid unnecessary conversational fluff."
    ),
    "debug": (
        "You are a principal debugging specialist and root-cause analyzer.\n"
        "- Carefully inspect the error stack trace, test output, or failing code using <tool:read>.\n"
        "- Find the exact root cause first, explain it in 1-2 bullet points.\n"
        "- Use <tool:edit> or <tool:write> to apply the minimal, robust fix.\n"
        "- Run verification commands with <tool:run> to confirm the fix."
    ),
    "review": (
        "You are a strict, senior code reviewer and security auditor.\n"
        "- Read and analyze code files using <tool:read>.\n"
        "- Categorize findings into: [Critical Vulnerability], [Warning/Bug], [Performance], [Style/Idiom].\n"
        "- Propose concrete code diffs or patches."
    ),
    "architect": (
        "You are a principal software architect and systems designer.\n"
        "- Analyze codebase structure using <tool:list> and <tool:find>.\n"
        "- Provide high-level architecture designs, API specifications, and phased implementation plans."
    ),
    "chat": (
        "You are a versatile, articulate, and friendly AI technical assistant."
    )
}


class WorkspaceToolExecutor:
    """Executes file operations and terminal commands in workspace."""

    @staticmethod
    def read_file(path_str: str, offset: int = 1, limit: int = 200) -> str:
        try:
            path = Path(path_str).resolve()
            if not path.exists():
                return f"Error: File '{path_str}' does not exist."
            if not path.is_file():
                return f"Error: Path '{path_str}' is not a file."

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, offset - 1)
            end_idx = min(total_lines, start_idx + limit)

            out_lines = [f"[File: {path_str} | Lines {start_idx + 1}-{end_idx} of {total_lines}]"]
            for idx in range(start_idx, end_idx):
                out_lines.append(f"{idx + 1:4d} | {lines[idx].rstrip()}")

            if end_idx < total_lines:
                out_lines.append(f"... ({total_lines - end_idx} more lines below)")
            return "\n".join(out_lines)
        except Exception as e:
            return f"Error reading file '{path_str}': {e}"

    @staticmethod
    def write_file(path_str: str, content: str) -> str:
        try:
            path = Path(path_str).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            line_count = len(content.splitlines())
            return f"Success: Wrote {len(content)} bytes ({line_count} lines) to '{path_str}'."
        except Exception as e:
            return f"Error writing file '{path_str}': {e}"

    @staticmethod
    def edit_file(path_str: str, search_block: str, replace_block: str) -> str:
        try:
            path = Path(path_str).resolve()
            if not path.exists() or not path.is_file():
                return f"Error: File '{path_str}' not found for editing."

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                original = f.read()

            clean_search = search_block.strip("\r\n")
            clean_replace = replace_block.strip("\r\n")

            if clean_search not in original:
                # Try fuzzy matching whitespace
                normalized_orig = re.sub(r"[ \t]+", " ", original)
                normalized_search = re.sub(r"[ \t]+", " ", clean_search)
                if normalized_search not in normalized_orig:
                    return f"Error: Search block not found in '{path_str}'. Ensure search block exactly matches target lines."

            updated = original.replace(clean_search, clean_replace, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)

            return f"Success: Applied patch to '{path_str}'."
        except Exception as e:
            return f"Error applying patch to '{path_str}': {e}"

    @staticmethod
    def run_command(command: str, timeout: int = 45) -> str:
        try:
            res = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout
            )
            out_parts = []
            if res.stdout:
                out_parts.append(res.stdout.strip())
            if res.stderr:
                out_parts.append(f"[stderr]\n{res.stderr.strip()}")
            if not out_parts:
                out_parts.append("(Command produced no output)")
            out_parts.append(f"[Exit code: {res.returncode}]")
            return "\n".join(out_parts)
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing command: {e}"

    @staticmethod
    def list_dir(path_str: str = ".") -> str:
        try:
            p = Path(path_str).resolve()
            if not p.exists() or not p.is_dir():
                return f"Error: Directory '{path_str}' does not exist."

            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            lines = [f"[Directory: {path_str}]"]
            for entry in entries[:60]:
                if entry.name.startswith((".git", "__pycache__", ".venv", "node_modules")):
                    continue
                icon = "📁" if entry.is_dir() else "📄"
                lines.append(f"  {icon} {entry.name}{'/' if entry.is_dir() else ''}")
            if len(entries) > 60:
                lines.append(f"... ({len(entries) - 60} more entries omitted)")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing directory: {e}"

    @staticmethod
    def find_files(pattern: str, root_dir: str = ".") -> str:
        try:
            matched = []
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules", "dist", "build")]
                for filename in files:
                    if fnmatch.fnmatch(filename, pattern):
                        rel_path = os.path.relpath(os.path.join(root, filename), root_dir)
                        matched.append(rel_path)
            if not matched:
                return f"No files found matching pattern '{pattern}'."
            return f"Found {len(matched)} matching files:\n" + "\n".join(f"  📄 {m}" for m in matched[:50])
        except Exception as e:
            return f"Error finding files: {e}"

    @staticmethod
    def grep_files(query: str, root_dir: str = ".") -> str:
        try:
            matches = []
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
                for filename in files:
                    if filename.endswith((".py", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".env.example", ".sh")):
                        filepath = os.path.join(root, filename)
                        try:
                            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                                for i, line in enumerate(f, 1):
                                    if query.lower() in line.lower():
                                        rel_path = os.path.relpath(filepath, root_dir)
                                        matches.append(f"{rel_path}:{i}: {line.strip()}")
                                        if len(matches) >= 35:
                                            break
                        except Exception:
                            pass
                if len(matches) >= 35:
                    break
            if not matches:
                return f"No matches found for '{query}'."
            return f"Found {len(matches)} matches:\n" + "\n".join(matches)
        except Exception as e:
            return f"Error in grep: {e}"


class HubbleChatSession:
    """Session management for the legacy CLI's interaction with the agent tool loop."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        mode: str = "code",
        auto_mode: bool = False
    ):
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        self.base_url = base_url
        self.api_key = ensure_api_key(api_key or API_KEY)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.mode = mode if mode in SYSTEM_PROMPTS else "code"
        self.system_prompt = SYSTEM_PROMPTS[self.mode]
        self.auto_mode = auto_mode  # Auto-execute tools without manual prompt confirmation
        self.history: List[Dict[str, str]] = []
        self.loaded_files: Dict[str, str] = {}  # filepath -> content

        timeout_cfg = httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)
        self.client = httpx.Client(timeout=timeout_cfg)

        # Token usage tracking
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.last_turn_stats: Dict[str, Any] = {}

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def reset_history(self):
        self.history = []

    def set_model(self, new_model: str):
        self.model = new_model.strip()

    def set_mode(self, mode: str) -> bool:
        if mode in SYSTEM_PROMPTS:
            self.mode = mode
            self.system_prompt = SYSTEM_PROMPTS[mode]
            return True
        return False

    def add_file(self, filepath: str) -> bool:
        path = Path(filepath).resolve()
        if not path.exists() or not path.is_file():
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            rel_path = os.path.relpath(path, os.getcwd())
            self.loaded_files[rel_path] = content
            return True
        except Exception:
            return False

    def drop_file(self, filepath: str) -> bool:
        for k in list(self.loaded_files.keys()):
            if filepath.lower() in k.lower():
                del self.loaded_files[k]
                return True
        return False

    def build_payload(self, stream: bool = True) -> Dict[str, Any]:
        system_content = self.system_prompt
        if self.loaded_files:
            file_context_parts = ["\n[Context: Files pinned into active session]"]
            for path, content in self.loaded_files.items():
                file_context_parts.append(f"--- File: {path} ---\n{content}\n--- End File ---")
            system_content += "\n" + "\n".join(file_context_parts)

        messages = [{"role": "system", "content": system_content}]
        messages.extend(self.history)

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def test_model(self, target_model: Optional[str] = None) -> Dict[str, Any]:
        """Test model connectivity and latency."""
        test_m = (target_model or self.model).strip()
        payload = {
            "model": test_m,
            "messages": [{"role": "user", "content": "Reply OK"}],
            "max_tokens": 10,
            "temperature": 0.1,
            "stream": False
        }
        start_t = time.time()
        try:
            resp = self.client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=12.0
            )
            lat = round((time.time() - start_t) * 1000)
            if resp.status_code == 200:
                raw = resp.text.strip()
                if "upstream returned 403" in raw or '"upstream_error"' in raw:
                    return {"model": test_m, "ok": False, "status": 200, "latency_ms": lat, "msg": "Upstream error inside 200"}
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    reasoning = choices[0].get("message", {}).get("reasoning_content", "").strip()
                    sample_text = content or reasoning
                    return {"model": test_m, "ok": True, "status": 200, "latency_ms": lat, "msg": sample_text[:50]}
                return {"model": test_m, "ok": True, "status": 200, "latency_ms": lat, "msg": "OK"}
            else:
                err_msg = ""
                try:
                    err_msg = resp.json().get("error", {}).get("message", "")
                except Exception:
                    err_msg = resp.text[:60]
                return {"model": test_m, "ok": False, "status": resp.status_code, "latency_ms": lat, "msg": err_msg or f"HTTP {resp.status_code}"}
        except Exception as e:
            lat = round((time.time() - start_t) * 1000)
            return {"model": test_m, "ok": False, "status": None, "latency_ms": lat, "msg": str(e)[:60]}

    def _extract_tools(self, text: str) -> List[Tuple[str, Dict[str, str], str]]:
        """Extract tool calls from text."""
        tools = []
        # Match self-closing: <tool:NAME param="val" />
        for m in re.finditer(r'<tool:(\w+)\s+([^>]*?)/>', text):
            t_name = m.group(1)
            params = dict(re.findall(r'(\w+)=["\']([^"\']*)["\']', m.group(2)))
            tools.append((t_name, params, ""))

        # Match blocks: <tool:NAME param="val">content</tool:NAME>
        for m in re.finditer(r'<tool:(\w+)(?:\s+([^>]*?))?>(.*?)</tool:\1>', text, re.DOTALL):
            t_name = m.group(1)
            params_str = m.group(2) or ""
            params = dict(re.findall(r'(\w+)=["\']([^"\']*)["\']', params_str))
            content = m.group(3)
            tools.append((t_name, params, content))

        return tools

    def _execute_single_tool(self, t_name: str, params: Dict[str, str], content: str) -> str:
        """Execute a parsed tool action."""
        if t_name == "read":
            path = params.get("path") or content.strip()
            offset = int(params.get("offset", 1))
            limit = int(params.get("limit", 200))
            return WorkspaceToolExecutor.read_file(path, offset, limit)

        elif t_name == "write":
            path = params.get("path", "").strip()
            if not path:
                return "Error: <tool:write> requires path=\"filename\" attribute."
            return WorkspaceToolExecutor.write_file(path, content)

        elif t_name == "edit":
            path = params.get("path", "").strip()
            if not path:
                return "Error: <tool:edit> requires path=\"filename\" attribute."
            match_patch = re.search(r'<<<<<<< SEARCH\r?\n(.*?)=======\r?\n(.*?)>>>>>>>', content, re.DOTALL)
            if not match_patch:
                return "Error: Invalid <tool:edit> format. Must contain <<<<<<< SEARCH ... ======= ... >>>>>>>"
            search_part = match_patch.group(1)
            replace_part = match_patch.group(2)
            return WorkspaceToolExecutor.edit_file(path, search_part, replace_part)

        elif t_name == "run":
            cmd = content.strip() or params.get("cmd", "")
            if not cmd:
                return "Error: No command specified for <tool:run>."
            return WorkspaceToolExecutor.run_command(cmd)

        elif t_name == "list":
            path = params.get("path") or content.strip() or "."
            return WorkspaceToolExecutor.list_dir(path)

        elif t_name == "find":
            pat = params.get("pattern") or content.strip() or "*"
            return WorkspaceToolExecutor.find_files(pat)

        elif t_name == "grep":
            q = params.get("query") or content.strip()
            path = params.get("path", ".")
            return WorkspaceToolExecutor.grep_files(q, path)

        return f"Error: Unknown tool '{t_name}'."

    def _stream_turn(self) -> Tuple[str, Dict[str, Any]]:
        """Stream a single model generation turn with real-time token rendering."""
        payload = self.build_payload(stream=True)
        full_response = []
        start_time = time.time()
        first_token_time = None
        server_usage = None
        delta_count = 0
        in_reasoning = False

        with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            json=payload
        ) as response:
            if response.status_code != 200:
                error_body = response.read().decode("utf-8", errors="replace")
                err_msg = ""
                try:
                    err_json = json.loads(error_body)
                    err_msg = err_json.get("error", {}).get("message", "")
                except Exception:
                    err_msg = error_body[:120]
                print(c(f"\n[-] API Error ({response.status_code}): {err_msg}", Style.RED))
                return "", {}

            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    raw_data = line[6:].strip()
                    if raw_data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw_data)
                        if "error" in chunk and chunk["error"]:
                            err_obj = chunk["error"]
                            err_txt = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                            print(c(f"\n[-] Model Stream Error: {err_txt}", Style.RED))
                            return "", {}

                        if "usage" in chunk and chunk["usage"]:
                            server_usage = chunk["usage"]

                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            reasoning_chunk = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                            token = delta.get("content", "")

                            if reasoning_chunk:
                                if not in_reasoning:
                                    in_reasoning = True
                                    sys.stdout.write(c("[Thinking: ", Style.DIM + Style.ITALIC))
                                sys.stdout.write(c(reasoning_chunk, Style.DIM))
                                sys.stdout.flush()
                                delta_count += 1

                            if token:
                                if in_reasoning:
                                    in_reasoning = False
                                    sys.stdout.write(c("]\n\n", Style.DIM + Style.ITALIC))
                                if first_token_time is None:
                                    first_token_time = time.time()
                                sys.stdout.write(token)
                                sys.stdout.flush()
                                full_response.append(token)
                                delta_count += 1
                    except json.JSONDecodeError:
                        continue

        if in_reasoning:
            sys.stdout.write(c("]\n", Style.DIM + Style.ITALIC))

        print()  # Final newline
        total_duration = time.time() - start_time
        ttft_ms = round((first_token_time - start_time) * 1000) if first_token_time else 0
        complete_text = "".join(full_response).strip()

        # Token calculation
        if server_usage and server_usage.get("prompt_tokens") is not None:
            prompt_tokens = int(server_usage.get("prompt_tokens", 0))
            completion_tokens = int(server_usage.get("completion_tokens", 0))
            total_tokens = int(server_usage.get("total_tokens", prompt_tokens + completion_tokens))
        else:
            messages_for_count = [{"role": "system", "content": self.system_prompt}] + self.history
            prompt_tokens = estimate_prompt_tokens(messages_for_count)
            completion_tokens = max(delta_count, estimate_tokens(complete_text))
            total_tokens = prompt_tokens + completion_tokens

        generation_duration = total_duration - (first_token_time - start_time) if first_token_time else total_duration
        tok_per_sec = (
            round(completion_tokens / generation_duration, 1)
            if generation_duration > 0 and completion_tokens > 0
            else round(completion_tokens / total_duration, 1) if total_duration > 0 else 0
        )

        turn_metrics = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "ttft_ms": ttft_ms,
            "total_duration": total_duration,
            "tok_per_sec": tok_per_sec
        }
        return complete_text, turn_metrics

    def execute_prompt(self, user_prompt: str, max_tool_iterations: int = 6):
        """Execute full Claude Code / Antigravity style autonomous agent loop."""
        self.history.append({"role": "user", "content": user_prompt})

        iteration = 0
        cumulative_prompt_tokens = 0
        cumulative_completion_tokens = 0
        cumulative_time = 0.0

        print(c(f"\nAI [{self.model}]", Style.BOLD + Style.MAGENTA), end=":\n")

        while iteration < max_tool_iterations:
            iteration += 1
            response_text, metrics = self._stream_turn()

            if not response_text:
                if self.history and self.history[-1]["role"] == "user":
                    self.history.pop()
                return

            self.history.append({"role": "assistant", "content": response_text})

            # Accumulate metrics
            cumulative_prompt_tokens += metrics.get("prompt_tokens", 0)
            cumulative_completion_tokens += metrics.get("completion_tokens", 0)
            cumulative_time += metrics.get("total_duration", 0.0)

            # Check if model requested any tool actions
            tools = self._extract_tools(response_text)
            if not tools:
                # No more tools requested; turn finished!
                break

            print(c(f"\n⚡ Detected {len(tools)} tool action(s) requested by model:", Style.BOLD + Style.CYAN))

            tool_outputs = []
            abort_loop = False

            for t_idx, (t_name, params, content) in enumerate(tools, 1):
                param_desc = ", ".join(f"{k}='{v}'" for k, v in params.items())
                body_preview = (content[:60] + "...") if len(content) > 60 else content.strip()
                action_str = f"{t_name}({param_desc}) {body_preview}".strip()

                # Confirmation handling unless auto_mode is on
                if not self.auto_mode:
                    print(c(f"\n┌─ [Tool Action {t_idx}/{len(tools)}]: {action_str}", Style.YELLOW + Style.BOLD))
                    try:
                        decision = input(c(f"└─ Execute action? [Y/n/always/stop]: ", Style.BOLD + Style.WHITE)).strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        decision = "stop"

                    if decision in ("stop", "abort", "q"):
                        print(c("[!] Aborting remaining tool executions.", Style.YELLOW))
                        abort_loop = True
                        break
                    elif decision in ("always", "all", "a"):
                        self.auto_mode = True
                        print(c("[+] Auto-mode ENABLED for remainder of session.", Style.GREEN + Style.BOLD))
                    elif decision in ("n", "no", "skip"):
                        print(c(f"[-] Skipped tool {t_name}.", Style.YELLOW))
                        tool_outputs.append(f"[Tool: {t_name}] Execution skipped by user.")
                        continue

                # Execute action
                print(c(f"⚡ Running: {t_name}...", Style.CYAN))
                res = self._execute_single_tool(t_name, params, content)

                # Pretty print summary of result
                first_lines = res.splitlines()[:5]
                preview_box = "\n".join(f"  {line}" for line in first_lines)
                if len(res.splitlines()) > 5:
                    preview_box += f"\n  ... ({len(res.splitlines()) - 5} lines omitted)"
                print(c(preview_box, Style.DIM))

                tool_outputs.append(f"--- Observation for <tool:{t_name}> ---\n{res}\n--- End Observation ---")

            if abort_loop or not tool_outputs:
                break

            # Feed tool observations back to assistant as user message
            observation_msg = "\n\n".join(tool_outputs)
            self.history.append({"role": "user", "content": observation_msg})
            print(c(f"\nAI [{self.model}] Continuing...", Style.BOLD + Style.MAGENTA), end=":\n")

        # Final Token Metrics Banner
        total_tok = cumulative_prompt_tokens + cumulative_completion_tokens
        speed = round(cumulative_completion_tokens / cumulative_time, 1) if cumulative_time > 0 else 0
        self.session_prompt_tokens += cumulative_prompt_tokens
        self.session_completion_tokens += cumulative_completion_tokens
        self.session_total_tokens += total_tok

        self.last_turn_stats = {
            "prompt_tokens": cumulative_prompt_tokens,
            "completion_tokens": cumulative_completion_tokens,
            "total_tokens": total_tok,
            "total_duration": cumulative_time,
            "tok_per_sec": speed
        }

        stats_bar = (
            f"Tokens: {c(f'📥 In: {cumulative_prompt_tokens}', Style.CYAN + Style.BOLD)} | "
            f"{c(f'📤 Out: {cumulative_completion_tokens}', Style.GREEN + Style.BOLD)} | "
            f"{c(f'📊 Total: {total_tok}', Style.WHITE + Style.BOLD)}  •  "
            f"Time: {cumulative_time:.2f}s | "
            f"{c(f'🚀 {speed} tok/s', Style.YELLOW)}"
        )
        print(f"\n{c('─' * 66, Style.DIM)}")
        print(stats_bar)
        print(c('─' * 66, Style.DIM))

    def compact_history(self):
        """Compact conversation history by retaining only last 3 turns and file context."""
        if len(self.history) <= 6:
            print(c("[i] History is already compact.", Style.YELLOW))
            return
        retained = self.history[-6:]
        summarized_count = len(self.history) - len(retained)
        self.history = [
            {"role": "system", "content": f"[Previous {summarized_count} conversation turns compacted for token efficiency]"}
        ] + retained
        print(c(f"[+] Successfully compacted context. Preserved latest {len(retained)//2} turns.", Style.GREEN))


def print_banner(session: HubbleChatSession):
    cached = load_cached_models()
    count_str = f"{len(cached)} verified models available" if cached else "33 models available"
    auto_tag = c("ON (Autonomous)", Style.GREEN + Style.BOLD) if session.auto_mode else c("OFF (Confirm Actions)", Style.YELLOW)

    banner = rf"""{Style.CYAN}{Style.BOLD}
  ___  ___ _  _ _   _ ___   ____ ___  ____  _____ 
 / _ \/ _ \ || | | | | _ ) / ___/ _ \|  _ \| ____|
/ /_\/ /_\ | || | |_| | _ \| |  | | | | | | |  _|  
|  _  |  _  |__   |  _  | _ <| |__| |_| | |_| | |___ 
|_| |_|_| |_|  |_| |_| |_|___/ \____\___/|____/|_____|
           Hubble Legacy CLI v3.0{Style.RESET}
{c('═' * 70, Style.CYAN)}
 💻 {c('Active Model', Style.BOLD)}   : {c(session.model, Style.GREEN + Style.BOLD)} (Default Coding Specialist)
 📁 {c('Workspace', Style.BOLD)}      : {c(os.getcwd(), Style.WHITE)}
 ⚡ {c('Auto-Run Tools', Style.BOLD)} : {auto_tag}
 ⚙️ {c('Mode', Style.BOLD)}           : {c(session.mode.upper(), Style.YELLOW + Style.BOLD)} ({session.system_prompt.splitlines()[0]})
 📦 {c('Registry', Style.BOLD)}       : {c(count_str, Style.DIM)}
{c('═' * 70, Style.CYAN)}
Tip: Type {c('/models', Style.BOLD)} to see all 33 models | {c('/help', Style.BOLD)} for commands
     Type {c('/auto on', Style.BOLD)} for autonomous code editing | {c('!<cmd>', Style.BOLD)} to run shell
{c('─' * 70, Style.DIM)}"""
    print(banner)


def print_help():
    help_text = f"""
{c('Hubble Legacy CLI Commands:', Style.BOLD + Style.CYAN)}

{c('Agentic Coding & Automation:', Style.BOLD + Style.WHITE)}
  {c('/auto [on|off]', Style.BOLD)}    Toggle autonomous tool execution without confirmation prompts
  {c('/add <file>', Style.BOLD)}       Pin file into conversation context (e.g. {c('/add main.py', Style.DIM)})
  {c('/drop <file>', Style.BOLD)}      Remove file from context
  {c('/files', Style.BOLD)}            List all files pinned in active context
  {c('/read <file>', Style.BOLD)}      Inspect file with line numbers directly in CLI
  {c('!<command>', Style.BOLD)}        Run shell command directly (e.g. {c('!git status', Style.DIM)}, {c('!pytest', Style.DIM)})
  {c('/run <command>', Style.BOLD)}    Alternative to {c('!<command>', Style.DIM)}
  {c('/compact', Style.BOLD)}          Compact conversation history to conserve token context

{c('Model & Latency:', Style.BOLD + Style.WHITE)}
  {c('/models [filter]', Style.BOLD)}   List categorized models with latency (e.g. {c('/models code', Style.DIM)})
  {c('/model <name|#>', Style.BOLD)}   Switch active model (e.g. {c('/model 1', Style.DIM)} or {c('/model codestral', Style.DIM)})
  {c('/test [name]', Style.BOLD)}      Benchmark latency and response health of model
  {c('/stats', Style.BOLD)}            Show token consumption and speed analytics

{c('Session & Persona:', Style.BOLD + Style.WHITE)}
  {c('/mode <mode>', Style.BOLD)}      Switch persona ({c('code', Style.GREEN)}, {c('debug', Style.RED)}, {c('review', Style.YELLOW)}, {c('architect', Style.BLUE)}, {c('chat', Style.CYAN)})
  {c('/temp [0.0-2.0]', Style.BOLD)}   Adjust model temperature (lower = more deterministic)
  {c('/clear', Style.BOLD)}            Clear conversation history
  {c('/copy [file]', Style.BOLD)}      Export entire session to markdown
  {c('<<<', Style.BOLD)} or {c('\"\"\"', Style.BOLD)}        Enter multi-line paste mode (end with {c('>>>', Style.BOLD)} or {c('\"\"\"', Style.BOLD)})
  {c('/exit', Style.BOLD)} or {c('/quit', Style.BOLD)}    Exit session
"""
    print(help_text)


def show_categorized_models(session: HubbleChatSession, filter_kw: str = "") -> List[str]:
    cached = load_cached_models()
    cache_map = {m["model"]: m.get("latency_ms", "-") for m in cached}
    all_listed = []

    print(c(f"\n=== AVAILABLE MODELS REGISTRY ===", Style.BOLD + Style.CYAN))

    # Print predefined categories
    for cat_name, models in MODEL_CATEGORIES.items():
        matching = [m for m in models if (not filter_kw or filter_kw.lower() in m.lower())]
        if not matching:
            continue

        print(c(f"\n📂 {cat_name}:", Style.BOLD + Style.YELLOW))
        for m in matching:
            all_listed.append(m)
            idx = len(all_listed)
            lat = f"{cache_map.get(m, '-')}ms"
            active_tag = c(" [ACTIVE]", Style.GREEN + Style.BOLD) if m == session.model else ""
            print(f"  {idx:>2}. {c(m, Style.BOLD):<42} {c(f'({lat:>6})', Style.DIM)}{active_tag}")

    # Additional cached models
    categorized_set = {m for sub in MODEL_CATEGORIES.values() for m in sub}
    extra_models = [m["model"] for m in cached if m["model"] not in categorized_set]
    if filter_kw:
        extra_models = [m for m in extra_models if filter_kw.lower() in m.lower()]

    if extra_models:
        print(c(f"\n📂 Other Verified Models ({len(extra_models)}):", Style.BOLD + Style.YELLOW))
        for m in extra_models:
            all_listed.append(m)
            idx = len(all_listed)
            lat = f"{cache_map.get(m, '-')}ms"
            active_tag = c(" [ACTIVE]", Style.GREEN + Style.BOLD) if m == session.model else ""
            print(f"  {idx:>2}. {c(m, Style.BOLD):<42} {c(f'({lat:>6})', Style.DIM)}{active_tag}")

    print(c(f"\nTip: Switch model by typing '/model <number>' (e.g. '/model 1')", Style.DIM))
    return all_listed


def run_interactive(session: HubbleChatSession):
    print_banner(session)
    active_view_models = load_cached_models()
    if not active_view_models:
        active_view_models = [{"model": m} for m in RELIABLE_MODELS]

    while True:
        try:
            cwd_name = os.path.basename(os.getcwd()) or os.getcwd()
            file_badge = f" [+{len(session.loaded_files)} files]" if session.loaded_files else ""
            auto_badge = c(" [AUTO]", Style.GREEN + Style.BOLD) if session.auto_mode else ""
            prompt_header = f"{c('╭─', Style.DIM)} {c('hubble', Style.BOLD + Style.CYAN)} {c(f'[{session.model}]', Style.GREEN)} {c(f'({session.mode})', Style.DIM)} 📁 {c(cwd_name, Style.WHITE)}{c(file_badge, Style.YELLOW)}{auto_badge}"
            prompt_input = f"{c('╰─❯', Style.DIM)} "

            print(f"\n{prompt_header}")
            user_input = input(prompt_input).strip()

            if not user_input:
                continue

            # Multi-line input mode
            if user_input in ('"""', "'''", "<<<"):
                print(c("[Multi-line mode active. Paste your code/prompt. End with '>>>' or '\"\"\"' on an empty line]", Style.YELLOW))
                lines = []
                while True:
                    try:
                        line = input("... ")
                        if line.strip() in ('"""', "'''", ">>>"):
                            break
                        lines.append(line)
                    except (KeyboardInterrupt, EOFError):
                        break
                user_input = "\n".join(lines).strip()
                if not user_input:
                    continue

            # Shell execution escape: !command or /run command
            if user_input.startswith("!") or user_input.startswith("/run "):
                cmd_line = user_input[1:].strip() if user_input.startswith("!") else user_input[5:].strip()
                if not cmd_line:
                    continue
                print(c(f"Running: {cmd_line}", Style.BOLD + Style.CYAN))
                out = WorkspaceToolExecutor.run_command(cmd_line)
                print(out)
                continue

            # Slash commands
            if user_input.startswith("/"):
                parts = user_input.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""

                if cmd in ("/exit", "/quit", "/q"):
                    print(c("\nExiting Hubble Legacy CLI. Happy coding!", Style.GREEN))
                    break

                elif cmd in ("/help", "/h", "/?"):
                    print_help()

                elif cmd in ("/auto", "/autorun"):
                    if arg.lower() in ("on", "true", "1", "yes"):
                        session.auto_mode = True
                        print(c("[+] Autonomous tool execution: ON (Actions will run without prompt)", Style.GREEN + Style.BOLD))
                    elif arg.lower() in ("off", "false", "0", "no"):
                        session.auto_mode = False
                        print(c("[+] Autonomous tool execution: OFF (Each action requires [Y/n] confirmation)", Style.YELLOW))
                    else:
                        session.auto_mode = not session.auto_mode
                        status_str = "ON (Autonomous)" if session.auto_mode else "OFF (Manual confirmation)"
                        print(c(f"[+] Autonomous tool execution toggled: {status_str}", Style.GREEN if session.auto_mode else Style.YELLOW))

                elif cmd in ("/clear", "/reset"):
                    session.reset_history()
                    print(c("[+] Conversation history cleared.", Style.GREEN))

                elif cmd in ("/compact", "/compress"):
                    session.compact_history()

                elif cmd in ("/models", "/list"):
                    listed = show_categorized_models(session, filter_kw=arg)
                    active_view_models = [{"model": m} for m in listed]

                elif cmd == "/model":
                    if not arg:
                        print(c(f"Active model: {session.model}", Style.BOLD))
                        print(c("Usage: /model <model_name> or /model <index_number>", Style.DIM))
                    else:
                        if arg.isdigit():
                            idx = int(arg) - 1
                            if 0 <= idx < len(active_view_models):
                                chosen = active_view_models[idx]["model"]
                                session.set_model(chosen)
                                print(c(f"[+] Switched active model to: {chosen}", Style.GREEN + Style.BOLD))
                            else:
                                print(c(f"[-] Invalid index. Must be 1 to {len(active_view_models)}", Style.RED))
                        else:
                            all_cached = load_cached_models()
                            pool = all_cached if all_cached else [{"model": m} for m in RELIABLE_MODELS]
                            matches = [m["model"] for m in pool if arg.lower() in m["model"].lower()]
                            exact = [m for m in matches if m.lower() == arg.lower()]
                            if exact:
                                session.set_model(exact[0])
                                print(c(f"[+] Switched active model to: {exact[0]}", Style.GREEN + Style.BOLD))
                            elif len(matches) == 1:
                                session.set_model(matches[0])
                                print(c(f"[+] Matched and switched active model to: {matches[0]}", Style.GREEN + Style.BOLD))
                            elif len(matches) > 1:
                                print(c(f"[i] Multiple models match '{arg}':", Style.YELLOW))
                                active_view_models = [{"model": m} for m in matches]
                                for i, m in enumerate(matches, 1):
                                    print(f"  {i}. {m}")
                                print(c("Tip: Type '/model <number>' to select one.", Style.DIM))
                            else:
                                session.set_model(arg)
                                print(c(f"[+] Set active model to: {arg}", Style.GREEN + Style.BOLD))

                elif cmd in ("/add", "/pin"):
                    if not arg:
                        print(c("Usage: /add <filepath>", Style.YELLOW))
                    else:
                        if session.add_file(arg):
                            print(c(f"[+] Pinned '{arg}' ({len(session.loaded_files[arg])} chars) into active context.", Style.GREEN))
                        else:
                            print(c(f"[-] Could not find or read file '{arg}'.", Style.RED))

                elif cmd in ("/drop", "/unpin"):
                    if not arg:
                        print(c("Usage: /drop <filepath>", Style.YELLOW))
                    else:
                        if session.drop_file(arg):
                            print(c(f"[+] Removed '{arg}' from active context.", Style.GREEN))
                        else:
                            print(c(f"[-] File '{arg}' not found in active context.", Style.RED))

                elif cmd in ("/files", "/context"):
                    if not session.loaded_files:
                        print(c("[i] No files currently pinned in context. Use /add <file> to load one.", Style.YELLOW))
                    else:
                        print(c(f"\nPinned Files in Context ({len(session.loaded_files)}):", Style.BOLD + Style.CYAN))
                        for f_path, content in session.loaded_files.items():
                            lines = len(content.splitlines())
                            size_kb = round(len(content) / 1024, 1)
                            print(f"  📄 {c(f_path, Style.BOLD)} ({lines} lines, {size_kb} KB)")

                elif cmd == "/read":
                    if not arg:
                        print(c("Usage: /read <filepath>", Style.YELLOW))
                    else:
                        print(WorkspaceToolExecutor.read_file(arg))

                elif cmd == "/mode":
                    if not arg:
                        print(f"Current mode: {session.mode} (Available: code, debug, review, architect, chat)")
                    else:
                        if session.set_mode(arg.lower()):
                            print(c(f"[+] Switched mode to: {arg.upper()}", Style.GREEN + Style.BOLD))
                        else:
                            print(c(f"[-] Unknown mode '{arg}'. Available: code, debug, review, architect, chat", Style.RED))

                elif cmd in ("/ls", "/dir"):
                    target_dir = arg if arg else "."
                    print(WorkspaceToolExecutor.list_dir(target_dir))

                elif cmd == "/test":
                    test_target = arg if arg else session.model
                    print(f"Testing connectivity and latency for {c(test_target, Style.BOLD)}...")
                    res = session.test_model(test_target)
                    if res["ok"]:
                        print(c(f"✅ SUCCESS ({res['latency_ms']}ms)", Style.GREEN + Style.BOLD), f"- Response: \"{res['msg']}\"")
                    else:
                        print(c(f"❌ FAILED ({res['latency_ms']}ms)", Style.RED + Style.BOLD), f"- Reason: {res['msg']}")

                elif cmd in ("/stats", "/tokens", "/status"):
                    print(c("\n--- Session Parameters & Token Metrics ---", Style.BOLD + Style.CYAN))
                    print(f"Base URL                : {session.base_url}")
                    print(f"Active Model            : {c(session.model, Style.BOLD + Style.GREEN)}")
                    print(f"Active Mode             : {c(session.mode.upper(), Style.YELLOW)}")
                    print(f"Auto-Execution          : {'ON (Autonomous)' if session.auto_mode else 'OFF'}")
                    print(f"Pinned Files in Context : {len(session.loaded_files)}")
                    print(f"Temperature             : {session.temperature}")
                    print(f"Max Tokens              : {session.max_tokens}")
                    print(f"Conversation Turns      : {len(session.history) // 2}")

                    if session.last_turn_stats:
                        lt = session.last_turn_stats
                        print(c("\nLast Turn Token Metrics:", Style.BOLD + Style.WHITE))
                        print(f"  • Input (Prompt) Tokens   : {c(str(lt['prompt_tokens']), Style.CYAN + Style.BOLD)}")
                        print(f"  • Output (Gen) Tokens     : {c(str(lt['completion_tokens']), Style.GREEN + Style.BOLD)}")
                        print(f"  • Total Tokens (Turn)     : {c(str(lt['total_tokens']), Style.WHITE + Style.BOLD)}")
                        speed_str = f"{lt['tok_per_sec']} tok/s"
                        print(f"  • Generation Speed        : {c(speed_str, Style.YELLOW)}")

                    print(c("\nCumulative Session Metrics:", Style.BOLD + Style.WHITE))
                    print(f"  • Total Input Tokens      : {c(str(session.session_prompt_tokens), Style.CYAN + Style.BOLD)}")
                    print(f"  • Total Output Tokens     : {c(str(session.session_completion_tokens), Style.GREEN + Style.BOLD)}")
                    print(f"  • Total Tokens Consumed   : {c(str(session.session_total_tokens), Style.WHITE + Style.BOLD)}")

                elif cmd in ("/history", "/hist"):
                    if not session.history:
                        print(c("[i] Conversation history is empty.", Style.YELLOW))
                    else:
                        print(c("\n--- Conversation History ---", Style.BOLD))
                        for i, msg in enumerate(session.history, 1):
                            role = c(msg['role'].upper(), Style.BOLD + (Style.CYAN if msg['role'] == 'user' else Style.MAGENTA))
                            print(f"{i}. {role}:\n   {msg['content'][:250]}\n")

                elif cmd == "/copy":
                    filename = arg if arg else f"chat_{int(time.time())}.md"
                    try:
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(f"# Developer Chat Export - {session.model}\n\n")
                            f.write(f"**Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                            for msg in session.history:
                                f.write(f"### {msg['role'].capitalize()}\n{msg['content']}\n\n")
                        print(c(f"[+] Conversation saved to {filename}", Style.GREEN))
                    except Exception as e:
                        print(c(f"[-] Failed to export: {e}", Style.RED))

                else:
                    print(c(f"[!] Unknown command '{cmd}'. Type /help for list of commands.", Style.YELLOW))

                continue

            # Standard prompt execution via Claude/Antigravity Agent Loop
            session.execute_prompt(user_input)

        except (KeyboardInterrupt, EOFError):
            print(c("\n\nExiting session. Bye!", Style.GREEN))
            break


def main():
    # The agentic CLI (hubble) replaced this one. `--legacy` keeps the old v3 behaviour.
    if "--legacy" not in sys.argv:
        from hubble.main import main as new_main
        new_main()
        return
    sys.argv.remove("--legacy")
    parser = argparse.ArgumentParser(
        description="Hubble Legacy CLI (v3.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Launch interactive coding CLI with codestral-latest:
  python chat_cli.py

  # Launch with auto-mode enabled (autonomous action execution):
  python chat_cli.py --auto

  # Single prompt execution:
  python chat_cli.py -p "Inspect config.py and summarize the available model categories"

  # Quick test of a model:
  python chat_cli.py --test codestral-latest
"""
    )
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("-p", "--prompt", help="Execute a single prompt and exit")
    parser.add_argument("--test", help="Test latency and connectivity for a model and exit")
    parser.add_argument("--base-url", default=BASE_URL, help=f"Base URL (default: {BASE_URL})")
    parser.add_argument("--api-key", default=None, help="API Key (default: loaded from .env file)")
    parser.add_argument("-t", "--temperature", type=float, default=DEFAULT_TEMPERATURE, help=f"Temperature (default: {DEFAULT_TEMPERATURE})")
    parser.add_argument("--tokens", type=int, default=DEFAULT_MAX_TOKENS, help=f"Max tokens (default: {DEFAULT_MAX_TOKENS})")
    parser.add_argument("--mode", default="code", choices=["code", "debug", "review", "architect", "chat"], help="Assistant mode (default: code)")
    parser.add_argument("--auto", action="store_true", help="Enable autonomous tool execution without interactive confirmations")

    args = parser.parse_args()
    active_key = ensure_api_key(args.api_key)

    session = HubbleChatSession(
        base_url=args.base_url,
        api_key=active_key,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.tokens,
        mode=args.mode,
        auto_mode=args.auto
    )

    if args.test:
        print(f"Testing model {c(args.test, Style.BOLD)} on {args.base_url}...")
        res = session.test_model(args.test)
        if res["ok"]:
            print(c(f"✅ Available ({res['latency_ms']}ms)", Style.GREEN + Style.BOLD))
            print(f"Sample response: \"{res['msg']}\"")
            sys.exit(0)
        else:
            print(c(f"❌ Unavailable ({res['latency_ms']}ms)", Style.RED + Style.BOLD))
            print(f"Reason: {res['msg']}")
            sys.exit(1)

    if args.prompt:
        session.execute_prompt(args.prompt)
        sys.exit(0)

    run_interactive(session)


if __name__ == "__main__":
    main()
