"""Runtime registry: discovery and capability-aware selection.

Selection never silently falls back: if no registered runtime supports every
capability a task requires, NoRuntimeAvailable names each rejection reason.
"""

from typing import Dict, List, Optional

from hubble.runtimes.protocol import DetectResult, RuntimeAdapter, RuntimeAdapterError


class NoRuntimeAvailable(RuntimeAdapterError):
    pass


class RuntimeRegistry:
    def __init__(self):
        self._adapters: Dict[str, RuntimeAdapter] = {}

    def register(self, adapter: RuntimeAdapter) -> RuntimeAdapter:
        rid = str(getattr(adapter, "runtime_id", "") or "")
        if not rid:
            raise ValueError("adapter has no runtime_id")
        self._adapters[rid] = adapter
        return adapter

    def get(self, runtime_id: str) -> Optional[RuntimeAdapter]:
        return self._adapters.get(runtime_id)

    def ids(self) -> List[str]:
        return sorted(self._adapters)

    def detect_all(self) -> List[DetectResult]:
        out: List[DetectResult] = []
        for rid in self.ids():
            try:
                out.append(self._adapters[rid].detect())
            except Exception as exc:  # detection must never crash the host
                out.append(DetectResult(runtime_id=rid, available=False,
                                        diagnostics=[f"detect failed: {exc}"]))
        return out

    def require(self, capabilities: List[str],
                preferred: Optional[str] = None) -> RuntimeAdapter:
        reasons: List[str] = []
        ordered = self.ids()
        if preferred and preferred in self._adapters:
            ordered = [preferred] + [r for r in ordered if r != preferred]
        for rid in ordered:
            adapter = self._adapters[rid]
            det = adapter.detect()
            if not det.available:
                reasons.append(f"{rid}: unavailable ({'; '.join(det.diagnostics) or 'no diagnostics'})")
                continue
            caps = adapter.capabilities()
            missing = [c for c in capabilities if not caps.supports(c)]
            if missing:
                reasons.append(f"{rid}: missing {', '.join(missing)}")
                continue
            return adapter
        raise NoRuntimeAvailable("no runtime satisfies "
                                 f"{capabilities}: " + "; ".join(reasons or ["registry empty"]))


def default_registry(include_fake: bool = False) -> RuntimeRegistry:
    """Adapters whose CLIs are verified on demand; import cost stays lazy.

    An adapter module that is not written yet (or whose optional dependency is
    absent) is skipped, never fatal: the registry reflects what exists.
    """
    reg = RuntimeRegistry()
    for module, cls_name in (
        ("muse_code", "MuseCodeAdapter"), ("codex", "CodexAdapter"),
        ("antigravity", "AntigravityAdapter"), ("opencode", "OpenCodeAdapter"),
        ("command_code", "CommandCodeAdapter"), ("claude_code", "ClaudeCodeAdapter"),
    ):
        try:
            module_obj = __import__(f"hubble.runtimes.adapters.{module}", fromlist=[cls_name])
            reg.register(getattr(module_obj, cls_name)())
        except Exception:
            continue
    if include_fake:
        from hubble.runtimes.adapters.fake import FakeAdapter
        reg.register(FakeAdapter())
    return reg
