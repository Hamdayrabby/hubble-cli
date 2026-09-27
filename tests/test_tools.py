from pathlib import Path

import pytest

from hubble.tools import (EditFile, Glob, Grep, ListDir, ReadFile, Shell, ToolContext, WriteFile, is_secret_path,
                            run_tool)


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n\nx = 1\nx = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("HUBBLE_API_KEY=secret\n", encoding="utf-8")
    return ToolContext(root=tmp_path)


def read(ctx, path):
    return run_tool(ReadFile(), {"path": path}, ctx)


def test_read_numbers_lines(ctx):
    out, err = read(ctx, "src/app.py")
    assert not err
    assert "1 | def add(a, b):" in out
    assert "lines 1-5 of 5" in out


def test_read_offset_limit_and_bad_int(ctx):
    out, err = run_tool(ReadFile(), {"path": "src/app.py", "offset": "2", "limit": 1}, ctx)
    assert not err and "2 |     return a + b" in out and "offset=3" in out
    out, err = run_tool(ReadFile(), {"path": "src/app.py", "offset": "abc"}, ctx)
    assert err and "integer" in out


def test_outside_workspace_blocked(ctx, tmp_path):
    out, err = read(ctx, "../outside.txt")
    assert err and "outside the workspace" in out
    out, err = read(ctx, str(Path(tmp_path).anchor) + "Windows/win.ini")
    assert err


def test_secret_files_blocked(ctx):
    out, err = read(ctx, ".env")
    assert err and "secrets" in out
    assert is_secret_path(Path("prod.env")) and is_secret_path(Path(".env.local"))
    assert not is_secret_path(Path(".env.example"))


def test_edit_requires_read_first(ctx):
    out, err = run_tool(EditFile(), {"path": "src/app.py", "old_string": "a + b", "new_string": "a - b"}, ctx)
    assert err and "read_file" in out


def test_edit_exact_unique(ctx):
    read(ctx, "src/app.py")
    out, err = run_tool(EditFile(), {"path": "src/app.py", "old_string": "a + b", "new_string": "a - b"}, ctx)
    assert not err, out
    assert "a - b" in (ctx.root / "src" / "app.py").read_text()


def test_edit_not_found_is_error(ctx):
    read(ctx, "src/app.py")
    out, err = run_tool(EditFile(), {"path": "src/app.py", "old_string": "return  a+b", "new_string": "z"}, ctx)
    assert err and "not found" in out


def test_edit_ambiguous_and_replace_all(ctx):
    read(ctx, "src/app.py")
    out, err = run_tool(EditFile(), {"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert err and "2 places" in out
    out, err = run_tool(EditFile(), {"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2",
                                     "replace_all": True}, ctx)
    assert not err
    assert (ctx.root / "src" / "app.py").read_text().count("x = 2") == 2


def test_edit_preserves_crlf(ctx):
    p = ctx.root / "win.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    read(ctx, "win.txt")
    out, err = run_tool(EditFile(), {"path": "win.txt", "old_string": "one\ntwo", "new_string": "1\n2"}, ctx)
    assert not err, out
    assert p.read_bytes() == b"1\r\n2\r\nthree\r\n"


def test_edit_detects_external_change(ctx):
    import os
    import time
    read(ctx, "src/app.py")
    p = ctx.root / "src" / "app.py"
    p.write_text("changed = True\n", encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    out, err = run_tool(EditFile(), {"path": "src/app.py", "old_string": "changed", "new_string": "c"}, ctx)
    assert err and "changed since" in out


def test_write_new_file_and_undo(ctx):
    ctx.begin_turn()
    out, err = run_tool(WriteFile(), {"path": "new/file.txt", "content": "hi\n"}, ctx)
    assert not err
    read(ctx, "src/app.py")
    run_tool(EditFile(), {"path": "src/app.py", "old_string": "a + b", "new_string": "a * b"}, ctx)
    restored = ctx.undo()
    assert set(restored) == {"new/file.txt", "src/app.py"}
    assert not (ctx.root / "new" / "file.txt").exists()
    assert "a + b" in (ctx.root / "src" / "app.py").read_text()


def test_write_existing_requires_read(ctx):
    out, err = run_tool(WriteFile(), {"path": "src/app.py", "content": "x"}, ctx)
    assert err and "read_file" in out


def test_grep_and_glob(ctx):
    out, err = run_tool(Grep(), {"pattern": r"def \w+"}, ctx)
    assert not err and "src/app.py:1:" in out
    out, err = run_tool(Grep(), {"pattern": "secret"}, ctx)
    assert "No matches" in out  # .env is excluded
    out, err = run_tool(Glob(), {"pattern": "*.py"}, ctx)
    assert out.strip() == "src/app.py"
    out, err = run_tool(Glob(), {"pattern": "src/**/*.py"}, ctx)
    assert "src/app.py" in out


def test_list_dir(ctx):
    out, err = run_tool(ListDir(), {}, ctx)
    assert "src/" in out


def test_shell_runs_in_root_without_stdin(ctx):
    out, err = run_tool(Shell(), {"command": "python -c \"import os; print(os.getcwd())\""}, ctx)
    assert not err
    assert str(ctx.root).lower() in out.lower()
    assert "[exit code 0]" in out


def test_missing_required_arg(ctx):
    out, err = run_tool(ReadFile(), {}, ctx)
    assert err and "path" in out


def test_edit_loose_trailing_whitespace(ctx):
    p = ctx.root / "ws.py"
    p.write_text("def f():  \n    return 1   \n\nprint(f())\n", encoding="utf-8")
    read(ctx, "ws.py")
    out, err = run_tool(EditFile(), {"path": "ws.py", "old_string": "\ndef f():\n    return 1\n",
                                     "new_string": "def f():\n    return 2\n"}, ctx)
    assert not err, out
    assert p.read_text() == "def f():\n    return 2\n\nprint(f())\n"


def test_precheck_rejects_before_approval(ctx):
    with pytest.raises(Exception, match="read_file"):
        EditFile().precheck({"path": "src/app.py", "old_string": "a + b", "new_string": "x"}, ctx)
    read(ctx, "src/app.py")
    with pytest.raises(Exception, match="not found"):
        EditFile().precheck({"path": "src/app.py", "old_string": "zzz", "new_string": "x"}, ctx)


def test_grep_never_returns_secrets(ctx):
    (ctx.root / "prod.ENV").write_text("TOKEN=abc\n", encoding="utf-8")
    (ctx.root / "k.pfx").write_text("TOKEN=abc\n", encoding="utf-8")
    out, err = run_tool(Grep(), {"pattern": "=", "path": ".env"}, ctx)
    assert err and "secrets" in out
    out, err = run_tool(Grep(), {"pattern": "TOKEN|AIHUB"}, ctx)
    assert "abc" not in out and "secret" not in out.split("(")[0]


def test_edit_refuses_non_utf8(ctx):
    p = ctx.root / "latin.txt"
    p.write_bytes("caf\xe9 = 1\n".encode("latin-1"))
    read(ctx, "latin.txt")
    out, err = run_tool(EditFile(), {"path": "latin.txt", "old_string": "= 1", "new_string": "= 2"}, ctx)
    assert err and "UTF-8" in out
    assert p.read_bytes() == "caf\xe9 = 1\n".encode("latin-1")


def test_shell_timeout_kills_tree(ctx):
    import time
    (ctx.root / "spawn.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n", encoding="utf-8")
    start = time.time()
    out, err = run_tool(Shell(), {"command": "python spawn.py", "timeout": 2}, ctx)
    assert err and "timed out" in out
    assert time.time() - start < 15


def test_web_fetch_blocks_private_addresses():
    from hubble.tools import ToolError
    from hubble.web import fetch_url
    for url in ("http://127.0.0.1:8080/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data",
                "http://192.168.1.1/", "file:///etc/passwd"):
        with pytest.raises(ToolError):
            fetch_url(url)


def test_html_to_text_and_ddg_parsing(monkeypatch):
    import httpx
    from hubble import web
    ext = web._TextExtractor("https://x")
    ext.feed("<html><head><title>Docs</title><script>evil()</script></head><body><nav>menu</nav>"
             "<h2>Install</h2><p>Run <code>pip install x</code>.</p><pre>a  b\n c</pre>"
             "<ul><li>one</li><li><a href='https://y.dev'>two</a></li></ul></body></html>")
    text = ext.text()
    assert ext.title == "Docs" and "evil" not in text and "menu" not in text
    assert "## Install" in text and "pip install x" in text and "a  b\n c" in text
    assert "- two (https://y.dev)" in text

    page = ('<div class="result results_links"><a rel="nofollow" class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python-httpx.org%2Fquickstart%2F&amp;rut=1">'
            'QuickStart - <b>HTTPX</b></a><a class="result__snippet" href="#">Streaming responses &amp; more</a></div>')
    monkeypatch.setattr(web.httpx, "post", lambda *a, **k: httpx.Response(200, text=page))
    res = web.search_duckduckgo("httpx", 5)
    assert res == [{"title": "QuickStart - HTTPX", "url": "https://www.python-httpx.org/quickstart/",
                    "snippet": "Streaming responses & more"}]


def test_web_permissions_per_domain():
    from hubble.permissions import Permissions
    from hubble.web import WebFetch
    t = WebFetch()
    assert t.target({"url": "https://Docs.Python.org/3/library/re.html"}) == "docs.python.org"
    p = Permissions("default")
    assert p.check("web_fetch", "web", "docs.python.org")[0] == "ask"
    assert p.always_rule("web_fetch", "web", "docs.python.org") == "web_fetch(docs.python.org)"
    assert p.check("web_fetch", "web", "docs.python.org")[0] == "allow"
    assert p.check("web_fetch", "web", "evil.example")[0] == "ask"
    p.mode = "plan"
    assert p.check("web_fetch", "web", "evil.example")[0] == "ask"      # allowed to ask while planning
    assert p.check("web_search", "read", "anything")[0] == "allow"
