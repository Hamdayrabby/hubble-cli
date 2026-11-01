"""Contract tests for the runtime layer: events, registry, fake adapter.

Deterministic and offline: no real harness CLI is launched here. Real-CLI
smoke evidence belongs in adapter-specific tests with explicit opt-in.
"""

import pytest

from hubble.runtimes import default_registry
from hubble.runtimes.adapters.fake import FakeAdapter
from hubble.runtimes.events import EventSequencer
from hubble.runtimes.protocol import RunSpec, RuntimeUnavailable, UnsupportedCapability
from hubble.runtimes.registry import NoRuntimeAvailable, RuntimeRegistry


def _spec(tmp_path, **kw):
    return RunSpec(prompt="do it", cwd=tmp_path, task_id="task-1", run_id="run-1", **kw)


def test_event_sequence_is_monotonic_and_versioned(tmp_path):
    seq = EventSequencer(task_id="t", run_id="r", runtime_id="fake")
    events = [seq.emit("process_started"), seq.emit("stdout", {"text": "x"}),
              seq.emit("completed", {"exit_code": 0})]
    assert [e.sequence for e in events] == [1, 2, 3]
    assert all(e.schema_version == 1 for e in events)
    assert events[1].to_dict()["payload"] == {"text": "x"}


def test_fake_adapter_happy_path(tmp_path):
    fake = FakeAdapter(script=["a", "b"])
    handle = fake.start(_spec(tmp_path))
    types = [e.event_type for e in fake.stream(handle)]
    assert types == ["process_started", "stdout", "stdout", "completed"]


def test_fake_adapter_failure_and_cancel(tmp_path):
    fake = FakeAdapter(exit_code=3)
    handle = fake.start(_spec(tmp_path))
    last = list(fake.stream(handle))[-1]
    assert last.event_type == "failed" and last.payload["exit_code"] == 3

    fake2 = FakeAdapter()
    handle2 = fake2.start(_spec(tmp_path))
    fake2.cancel(handle2)
    assert list(fake2.stream(handle2))[-1].event_type == "cancelled"


def test_fake_adapter_unsupported_send_and_resume(tmp_path):
    fake = FakeAdapter()
    handle = fake.start(_spec(tmp_path))
    with pytest.raises(UnsupportedCapability):
        fake.send(handle, "hello")
    with pytest.raises(UnsupportedCapability):
        fake.resume("sess-1", _spec(tmp_path))


def test_unavailable_adapter_fails_loudly(tmp_path):
    fake = FakeAdapter(available=False)
    assert fake.detect().available is False
    with pytest.raises(RuntimeUnavailable):
        fake.start(_spec(tmp_path))


def test_registry_capability_matching_and_reasons(tmp_path):
    reg = RuntimeRegistry()
    reg.register(FakeAdapter())
    assert reg.require(["one_shot", "json_stream"]).runtime_id == "fake"
    with pytest.raises(NoRuntimeAvailable) as exc:
        reg.require(["resume"])
    assert "resume" in str(exc.value)

    empty = RuntimeRegistry()
    with pytest.raises(NoRuntimeAvailable):
        empty.require(["one_shot"])


def test_default_registry_always_has_fake_when_asked():
    reg = default_registry(include_fake=True)
    assert "fake" in reg.ids()
    assert all(d.runtime_id for d in reg.detect_all())


def test_child_env_is_allowlisted(tmp_path):
    spec = _spec(tmp_path, env_extra={"HARNESS_HOME": "/tmp/harness"})
    env = spec.child_env(base={"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "nope",
                               "HUBBLE_API_KEY": "nope"})
    assert env["PATH"] == "/bin" and env["HARNESS_HOME"] == "/tmp/harness"
    assert "AWS_SECRET_ACCESS_KEY" not in env and "HUBBLE_API_KEY" not in env
