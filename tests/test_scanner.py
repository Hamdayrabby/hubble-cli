import json

import httpx

import hubble.scanner as scanner_mod
from hubble.scanner import ModelScanner, _probe

BASE = "https://gw.example/v1"


def ok(content="hi there", reasoning=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": finish}]})


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_counts_reasoning_only_reply_as_available():
    with client_for(lambda req: ok(content="", reasoning="thinking about hi")) as c:
        r = _probe(c, BASE, {}, "reasoner", 5)
    assert r["available"] is True and not r["transient"]


def test_probe_counts_length_cutoff_as_available():
    with client_for(lambda req: ok(content="", finish="length")) as c:
        r = _probe(c, BASE, {}, "reasoner", 5)
    assert r["available"] is True


def test_probe_marks_rate_limit_and_timeout_transient_but_404_not():
    with client_for(lambda req: httpx.Response(429)) as c:
        assert _probe(c, BASE, {}, "m", 5)["transient"] is True

    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)
    with client_for(boom) as c:
        assert _probe(c, BASE, {}, "m", 5)["transient"] is True

    with client_for(lambda req: httpx.Response(404)) as c:
        r = _probe(c, BASE, {}, "m", 5)
    assert r["available"] is False and r["transient"] is False


def run_scan(tmp_path, monkeypatch, handler, previous=None):
    out = tmp_path / "scan.json"
    if previous is not None:
        out.write_text(json.dumps(previous), encoding="utf-8")
    real_client = httpx.Client
    monkeypatch.setattr(scanner_mod.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    s = ModelScanner(BASE, "key", output=out, retry_delays=(0, 0))
    s._run()
    return s, json.loads(out.read_text(encoding="utf-8"))


def models_listing(*ids):
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


def test_rate_limited_model_is_retried_and_recovers(tmp_path, monkeypatch):
    calls = {"flaky": 0}

    def handler(req):
        if req.url.path.endswith("/models"):
            return models_listing("good", "flaky", "dead")
        model = json.loads(req.content)["model"]
        if model == "flaky":
            calls["flaky"] += 1
            return httpx.Response(429) if calls["flaky"] == 1 else ok()
        return ok() if model == "good" else httpx.Response(404)

    s, data = run_scan(tmp_path, monkeypatch, handler)
    assert s.status == "done"
    assert {m["model"] for m in data["working_models"]} == {"good", "flaky"}
    assert s.working == 2


def test_still_rate_limited_is_unknown_not_unavailable(tmp_path, monkeypatch):
    def handler(req):
        if req.url.path.endswith("/models"):
            return models_listing("good", "busy")
        return ok() if json.loads(req.content)["model"] == "good" else httpx.Response(503)

    _, data = run_scan(tmp_path, monkeypatch, handler)
    busy = next(r for r in data["all_results"] if r["model"] == "busy")
    assert busy["available"] is None
    assert "busy" not in {m["model"] for m in data["working_models"]}


def test_previously_working_model_kept_on_transient_failure(tmp_path, monkeypatch):
    previous = {"working_models": [{"model": "busy", "latency_ms": 321, "sample": "hello"}]}

    def handler(req):
        if req.url.path.endswith("/models"):
            return models_listing("good", "busy")
        return ok() if json.loads(req.content)["model"] == "good" else httpx.Response(429)

    _, data = run_scan(tmp_path, monkeypatch, handler, previous)
    kept = next(m for m in data["working_models"] if m["model"] == "busy")
    assert kept["stale"] is True and kept["latency_ms"] == 321


def test_spurious_401_is_retried_but_persistent_401_is_unavailable(tmp_path, monkeypatch):
    calls = {"once": 0}

    def handler(req):
        if req.url.path.endswith("/models"):
            return models_listing("once", "always")
        model = json.loads(req.content)["model"]
        if model == "once":
            calls["once"] += 1
            return httpx.Response(401) if calls["once"] == 1 else ok()
        return httpx.Response(401)

    _, data = run_scan(tmp_path, monkeypatch, handler)
    assert {m["model"] for m in data["working_models"]} == {"once"}
    always = next(r for r in data["all_results"] if r["model"] == "always")
    assert always["available"] is False


def test_provider_models_shows_transient_as_unknown(tmp_path, monkeypatch):
    import hubble.providers as prov
    path = tmp_path / "p.json"
    path.write_text(json.dumps({
        "all_ids": ["a", "b", "c", "d"],
        "working_models": [{"model": "a", "latency_ms": 10}],
        "all_results": [{"model": "a", "available": True}, {"model": "b", "available": None, "reason": "HTTP 429"},
                        {"model": "c", "available": False, "reason": "HTTP 404"}],
    }), encoding="utf-8")
    monkeypatch.setattr(prov, "scan_file", lambda name: path)
    by = {m["model"]: m for m in prov.provider_models("other")}
    assert by["a"]["available"] is True
    assert by["b"]["available"] is None and by["b"]["note"] == "HTTP 429"
    assert by["c"]["available"] is False
    assert by["d"]["available"] is None and by["d"]["category"] == "Not checked"
