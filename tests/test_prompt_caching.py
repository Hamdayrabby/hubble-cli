from hubble.anthropic_provider import AnthropicProvider


def test_3_tier_prompt_caching_breakpoints():
    p = AnthropicProvider("https://api.anthropic.com", "sk-ant-test")
    messages = [
        {"role": "system", "content": "You are a helpful coding agent."},
        {"role": "user", "content": "Turn 1: please list files"},
        {"role": "assistant", "content": "Here are the files."},
        {"role": "user", "content": "Turn 2: please edit foo.py"},
    ]
    tools = [
        {"type": "function", "function": {"name": "read_file", "description": "read", "parameters": {}}},
        {"type": "function", "function": {"name": "edit_file", "description": "edit", "parameters": {}}},
    ]

    params = p._params("claude-sonnet-4-5", messages, tools, max_tokens=4096)

    # Breakpoint 1: System prompt
    assert "system" in params
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}

    # Breakpoint 2: Tool catalog (on last tool)
    assert "tools" in params
    assert len(params["tools"]) == 2
    assert "cache_control" not in params["tools"][0]
    assert params["tools"][1]["cache_control"] == {"type": "ephemeral"}

    # Breakpoint 3: Rolling conversation turn anchor (on 2nd-to-last user turn)
    msgs = params["messages"]
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 2
    # The first user message (Turn 1) is user_msgs[-2]
    first_user_content = user_msgs[0]["content"]
    assert first_user_content[-1]["cache_control"] == {"type": "ephemeral"}
    # The latest user message (Turn 2) is dynamic/uncached
    latest_user_content = user_msgs[1]["content"]
    assert "cache_control" not in latest_user_content[-1]
