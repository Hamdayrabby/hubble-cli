import subprocess
import sys
import pytest

from hubble.sandbox import (
    WindowsJobSandbox,
    has_windows_job_object,
    parse_memory_limit_bytes,
)


def test_parse_memory_limit_bytes():
    assert parse_memory_limit_bytes("1g") == 1024 * 1024 * 1024
    assert parse_memory_limit_bytes("512m") == 512 * 1024 * 1024
    assert parse_memory_limit_bytes("2gb") == 2 * 1024 * 1024 * 1024
    assert parse_memory_limit_bytes("100kb") == 100 * 1024
    assert parse_memory_limit_bytes("1048576") == 1048576
    assert parse_memory_limit_bytes(None) is None
    assert parse_memory_limit_bytes("invalid") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_job_sandbox_lifecycle():
    assert has_windows_job_object() is True
    job = WindowsJobSandbox(memory_limit_bytes=512 * 1024 * 1024, max_processes=32)
    assert job.handle is not None

    proc = subprocess.Popen(["cmd.exe", "/c", "echo test_job"], stdout=subprocess.PIPE)
    assigned = job.assign_process(proc)
    assert assigned is True
    stdout, _ = proc.communicate()
    assert "test_job" in stdout.decode()
    job.close()
    assert job.handle is None
