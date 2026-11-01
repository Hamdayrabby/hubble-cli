"""ProcRun owns the child console lifecycle (no Ctrl+C propagation)."""

from hubble.runtimes.adapters import _proc


def test_proc_run_detaches_console_group(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            import io
            self.stdout = io.StringIO("")
            self.pid = 1

        def poll(self):
            return 0

    monkeypatch.setattr(_proc.subprocess, "Popen", FakePopen)
    _proc.ProcRun(["echo", "hi"], cwd=tmp_path, env={})
    import sys
    if sys.platform == "win32":
        assert seen.get("creationflags") == _proc.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen.get("start_new_session") is True
    assert seen.get("stdin") == _proc.subprocess.DEVNULL
