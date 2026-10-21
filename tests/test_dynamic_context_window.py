import json

import pytest

from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.settings import detect_context_window
from hubble.tools import ToolContext


def test_detect_context_window_frontier_and_open_models():
    # 2M models
    assert detect_context_window("gemini-1.5-pro") == 2_000_000
    assert detect_context_window("gemini-2.5-pro") == 2_000_000
    assert detect_context_window("google/gemini-ultra") == 2_000_000

    # 1M models
    assert detect_context_window("gemini-1.5-flash") == 1_000_000
    assert detect_context_window("gemini-2.0-flash") == 1_000_000

    # 256k models
    assert detect_context_window("codestral-latest") == 256_000
    assert detect_context_window("codestral-2508") == 256_000

    # 200k models
    assert detect_context_window("claude-3-5-sonnet-20241022") == 200_000
    assert detect_context_window("claude-3-opus-20240229") == 200_000
    assert detect_context_window("claude-3-7-sonnet") == 200_000

    # 128k models
    assert detect_context_window("gpt-4o") == 128_000
    assert detect_context_window("gpt-4o-mini") == 128_000
    assert detect_context_window("o1-preview") == 128_000
    assert detect_context_window("o3-mini") == 128_000
    assert detect_context_window("meta-llama/llama-3.1-70b-instruct") == 128_000
    assert detect_context_window("llama-3.2-11b-vision-instruct") == 128_000
    assert detect_context_window("qwen-2.5-coder-32b") == 128_000
    assert detect_context_window("deepseek-v3") == 128_000
    assert detect_context_window("deepseek-r1") == 128_000

    # 64k models
    assert detect_context_window("deepseek-coder-33b") == 64_000

    # 32k models
    assert detect_context_window("ministral-8b-latest") == 32_768
    assert detect_context_window("open-mistral-nemo") == 32_768
    assert detect_context_window("intern-s2-preview") == 32_768
    assert detect_context_window("phi-3-mini-128k") == 128_000 or detect_context_window("phi-3-mini") == 32_768

    # 16k models
    assert detect_context_window("gpt-3.5-turbo-16k") == 16_384
    assert detect_context_window("mistral-7b-instruct") == 16_384

    # 8k models
    assert detect_context_window("gpt-4-0613") == 8_192

    # Default fallback for unknown
    assert detect_context_window("custom-unknown-model", default=128000) == 128_000


def test_agent_effective_context_window_auto(tmp_path):
    ctx = ToolContext(root=tmp_path)

    # 1. Claude model with default settings -> detects 200,000
    agent_claude = Agent(None, {"model": "claude-3-5-sonnet", "context_window": 128000}, ctx, Permissions(), Events())
    assert agent_claude.effective_context_window() == 200_000

    # 2. Gemini model with default settings -> detects 2,000,000
    agent_gemini = Agent(None, {"model": "gemini-1.5-pro", "context_window": 128000}, ctx, Permissions(), Events())
    assert agent_gemini.effective_context_window() == 2_000_000

    # 3. Explicit non-default context_window -> preserved
    agent_custom = Agent(None, {"model": "claude-3-5-sonnet", "context_window": 50000}, ctx, Permissions(), Events())
    assert agent_custom.effective_context_window() == 50_000

    # 4. context_window_auto: False -> returns exact context_window
    agent_fixed = Agent(None, {"model": "gemini-1.5-pro", "context_window": 128000, "context_window_auto": False}, ctx, Permissions(), Events())
    assert agent_fixed.effective_context_window() == 128_000


def test_agent_context_ratio_dynamic(tmp_path):
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "claude-3-5-sonnet", "context_window": 128000}, ctx, Permissions(), Events())
    agent.context_tokens = 50_000

    # 50,000 / 200,000 = 0.25 (25%)
    assert agent.context_ratio() == pytest.approx(0.25)


def test_subagent_adaptive_token_budget(tmp_path):
    # When a subagent runs on a 32k model (ministral-8b-latest) and no explicit budget is passed,
    # its budget should adaptively scale to avoid compaction thrashing.
    sub_models_tested = []

    class ScriptedSubProvider:
        def __init__(self):
            self.parent_turns = [
                TurnResult(tool_calls=[ToolCall("t1", "task", json.dumps({
                    "description": "explore",
                    "prompt": "explore",
                    "model": "ministral-8b-latest",
                }))]),
                TurnResult(text="Done."),
            ]

        def stream(self, model, messages, **kw):
            content = str(messages[0].get("content", ""))
            if "sub-agent inside Hubble" in content:
                sub_models_tested.append(model)
                return TurnResult(text="Exploration complete.")
            return self.parent_turns.pop(0)

    ctx = ToolContext(root=tmp_path)
    provider = ScriptedSubProvider()
    agent = Agent(provider, {"model": "gpt-4o", "max_turns": 4, "max_tokens": 100, "context_window": 128000,
                             "subagent_policy": "auto"},
                  ctx, Permissions(), Events())
    agent.run("explore with ministral")

    assert "ministral-8b-latest" in sub_models_tested
    tool_out = next(m for m in agent.messages if m.get("role") == "tool")["content"]
    assert "Exploration complete." in tool_out
