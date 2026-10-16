import httpx
from hubble.web import search_gateway, web_search


def test_search_gateway_success(monkeypatch):
    called = []

    def fake_post(url, json=None, headers=None, timeout=None):
        called.append((url, json, headers))
        return httpx.Response(200, json={
            "results": [
                {"title": "Result 1", "url": "https://example.com/1", "content": "Snippet 1"},
                {"title": "Result 2", "url": "https://example.com/2", "snippet": "Snippet 2"},
            ]
        })

    monkeypatch.setattr(httpx, "post", fake_post)

    res = search_gateway("python async", limit=2, search_url="https://search.example.com/v1/search", api_key="sk-test")
    assert res is not None
    assert len(res) == 2
    assert res[0]["title"] == "Result 1"
    assert res[0]["url"] == "https://example.com/1"
    assert res[0]["snippet"] == "Snippet 1"
    assert "https://search.example.com/v1/search" in called[0][0]
    assert called[0][2]["Authorization"] == "Bearer sk-test"


def test_search_gateway_fallback_on_404(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(404, text="Not found")

    monkeypatch.setattr(httpx, "post", fake_post)

    # When gateway returns 404, search_gateway returns None
    res = search_gateway("test", limit=5, search_url="https://search.example.com/v1/search", api_key="sk-test")
    assert res is None


def test_web_search_routes_to_gateway(monkeypatch):
    def fake_gateway(query, limit, search_url=None, api_key=None):
        return [{"title": "Gateway Hit", "url": "https://gateway.com", "snippet": "Found via proxy"}]

    monkeypatch.setattr("hubble.web.search_gateway", fake_gateway)

    results = web_search("query", limit=1, config={"search_url": "https://search.example.com/v1/search", "api_key": "sk-test"})
    assert len(results) == 1
    assert results[0]["title"] == "Gateway Hit"
