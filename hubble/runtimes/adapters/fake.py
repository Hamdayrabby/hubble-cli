"""Deterministic fake adapter: the contract every real adapter is tested against.

No process, no network, no model. ``script`` lines are replayed as stdout
events; tests steer exit code, failure text, and cancellation precisely.
"""

import itertools
from typing import Iterator, List

from hubble.runtimes.events import EventSequencer, RuntimeEvent
from hubble.runtimes.protocol import (
    DetectResult, RunHandle, RunSpec, RuntimeCapabilities,
    RuntimeUnavailable, UnsupportedCapability,
)


class FakeAdapter:
    runtime_id = "fake"

    def __init__(self, script: List[str] = None, exit_code: int = 0,
                 available: bool = True, version: str = "fake-1.0"):
        self._script = list(script or ["fake: working", "fake: done"])
        self._exit_code = exit_code
        self._available = available
        self._version = version
        self._counter = itertools.count(1)
        self.started: List[RunSpec] = []
        self.sent: List[str] = []

    def detect(self) -> DetectResult:
        if not self._available:
            return DetectResult(runtime_id=self.runtime_id, available=False,
                                diagnostics=["fake adapter disabled for this test"])
        return DetectResult(runtime_id=self.runtime_id, available=True,
                            executable="fake", version=self._version)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(one_shot=True, interactive=False,
                                   json_stream=True, resume=False, cancel=True)

    def start(self, spec: RunSpec) -> RunHandle:
        if not self._available:
            raise RuntimeUnavailable("fake adapter disabled for this test")
        self.started.append(spec)
        n = next(self._counter)
        return RunHandle(runtime_id=self.runtime_id, run_id=spec.run_id or f"run-{n}",
                         task_id=spec.task_id, pid=0, session_ref="")

    def stream(self, handle: RunHandle) -> Iterator[RuntimeEvent]:
        seq = EventSequencer(task_id=handle.task_id, run_id=handle.run_id,
                             runtime_id=self.runtime_id)
        yield seq.emit("process_started", {"pid": handle.pid})
        for line in self._script:
            if handle.cancelled:
                yield seq.emit("cancelled", {})
                return
            yield seq.emit("stdout", {"text": line})
        if handle.cancelled:
            yield seq.emit("cancelled", {})
        elif self._exit_code == 0:
            yield seq.emit("completed", {"exit_code": 0})
        else:
            yield seq.emit("failed", {"exit_code": self._exit_code})

    def send(self, handle: RunHandle, message: str) -> None:
        raise UnsupportedCapability("fake runtime is one-shot; send is unsupported")

    def cancel(self, handle: RunHandle) -> str:
        handle.cancelled = True
        return "fake run cancelled"

    def resume(self, session_ref: str, spec: RunSpec) -> RunHandle:
        raise UnsupportedCapability("fake runtime cannot resume; start a new run")

    def close(self, handle: RunHandle) -> None:
        return None
