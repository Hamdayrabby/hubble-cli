"""OpenCode/Command Code are now full adapters; keep a detect-only guard here.

Their process behavior is covered with faked processes in
tests/test_opencode_adapter.py and tests/test_command_code_adapter.py.
These tests never spawn a real CLI.
"""

from hubble.runtimes.adapters.command_code import CommandCodeAdapter
from hubble.runtimes.adapters.opencode import OpenCodeAdapter
from hubble.runtimes.protocol import DetectResult


def test_verified_adapters_expose_capabilities_without_spawning():
    for adapter in (OpenCodeAdapter(), CommandCodeAdapter()):
        result = adapter.detect()
        assert isinstance(result, DetectResult) and result.runtime_id
        caps = adapter.capabilities()
        assert caps.supports("one_shot") is True
        assert caps.supports("json_stream") is True
        assert caps.supports("resume") is True
