import pytest
from hubble.provider import parse_arguments, repair_dirty_json


def test_parse_arguments_standard():
    raw = '{"path": "foo.py", "replace_all": false}'
    res = parse_arguments(raw)
    assert res == {"path": "foo.py", "replace_all": False}


def test_parse_arguments_trailing_comma():
    raw = '{"path": "foo.py", "replace_all": false,}'
    res = parse_arguments(raw)
    assert res == {"path": "foo.py", "replace_all": False}


def test_parse_arguments_python_literals():
    raw = '{"path": "foo.py", "replace_all": False, "flag": True, "opt": None}'
    res = parse_arguments(raw)
    assert res["replace_all"] is False
    assert res["flag"] is True
    assert res["opt"] is None


def test_parse_arguments_unescaped_newlines():
    raw = '{"command": "echo line 1\necho line 2"}'
    res = parse_arguments(raw)
    assert "echo line 1\necho line 2" in res["command"]


def test_parse_arguments_single_quotes():
    raw = "{'path': 'models/user.py', 'old_string': 'def foo():', 'count': 5}"
    res = parse_arguments(raw)
    assert res["path"] == "models/user.py"
    assert res["old_string"] == "def foo():"
    assert res["count"] == 5


def test_parse_arguments_apostrophe_in_value():
    raw = "{'path': 'models/user.py', 'comment': \"don't stop\"}"
    res = parse_arguments(raw)
    assert res["path"] == "models/user.py"
    assert res["comment"] == "don't stop"


def test_parse_arguments_fenced_markdown():
    raw = """Here is the tool call:
```json
{
  "path": "app.py",
  "content": "print('hello')"
}
```
"""
    res = parse_arguments(raw)
    assert res["path"] == "app.py"
    assert res["content"] == "print('hello')"


def test_parse_arguments_unclosed_braces():
    raw = '{"path": "broken.py", "content": "incomplete"'
    res = parse_arguments(raw)
    assert res["path"] == "broken.py"
    assert res["content"] == "incomplete"


def test_parse_arguments_invalid_raises():
    with pytest.raises(ValueError, match="not a valid JSON object"):
        parse_arguments("not a json at all")
