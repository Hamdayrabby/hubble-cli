"""Web tools: web_search (DuckDuckGo by default, Brave/Tavily with an API key) and web_fetch.

web_fetch refuses loopback/private/link-local addresses, so the model cannot probe the
user's local network or cloud metadata endpoints.
"""

import html
import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from hubble.tools import Tool, ToolContext, ToolError

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 Hubble-CLI")
TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
MAX_DOWNLOAD = 3_000_000
MAX_RESULTS = 10


# ----- search backends ------------------------------------------------------

def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def search_duckduckgo(query: str, limit: int) -> List[Dict[str, str]]:
    resp = httpx.post("https://html.duckduckgo.com/html/", data={"q": query},
                      headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
    if resp.status_code != 200:
        raise ToolError(f"DuckDuckGo returned HTTP {resp.status_code}")
    page = resp.text
    if "anomaly" in page.lower() and "result__a" not in page:
        raise ToolError("DuckDuckGo asked for a captcha; set a Brave or Tavily API key for reliable search")
    results = []
    blocks = re.split(r'<div[^>]+class="[^"]*result(?:s_links)?[^"]*"', page)
    for block in blocks:
        link = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = html.unescape(link.group(1))
        if "duckduckgo.com/l/" in href:  # redirect wrapper: real URL is in uddg=
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        results.append({"title": _strip_tags(link.group(2)), "url": href,
                        "snippet": _strip_tags(snippet.group(1)) if snippet else ""})
        if len(results) >= limit:
            break
    return results


def search_brave(query: str, limit: int, key: str) -> List[Dict[str, str]]:
    resp = httpx.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": limit},
                     headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise ToolError(f"Brave Search returned HTTP {resp.status_code}")
    items = (resp.json().get("web") or {}).get("results") or []
    return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": _strip_tags(i.get("description", ""))}
            for i in items[:limit]]


def search_tavily(query: str, limit: int, key: str) -> List[Dict[str, str]]:
    resp = httpx.post("https://api.tavily.com/search", json={"api_key": key, "query": query, "max_results": limit},
                      timeout=TIMEOUT)
    if resp.status_code != 200:
        raise ToolError(f"Tavily returned HTTP {resp.status_code}")
    return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": (i.get("content") or "")[:300]}
            for i in (resp.json().get("results") or [])[:limit]]


def web_search(query: str, limit: int = 8, config: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    cfg = config or {}
    brave = cfg.get("brave_api_key") or os.environ.get("BRAVE_API_KEY")
    tavily = cfg.get("tavily_api_key") or os.environ.get("TAVILY_API_KEY")
    engine = (cfg.get("engine") or "auto").lower()
    if engine in ("brave", "auto") and brave:
        return search_brave(query, limit, brave)
    if engine in ("tavily", "auto") and tavily:
        return search_tavily(query, limit, tavily)
    try:
        return search_duckduckgo(query, limit)
    except httpx.HTTPError as e:
        raise ToolError(f"search failed: {type(e).__name__}: {e}")


# ----- fetch -----------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "template", "head"}
    BLOCK = {"p", "div", "section", "article", "main", "br", "li", "ul", "ol", "table", "tr", "pre",
             "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "header", "aside", "dd", "dt"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: List[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._href: Optional[str] = None
        self._link_start = 0
        self._pre = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self._pre += 1
            self.out.append("\n```\n")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith("http") else None
            self._link_start = len(self.out)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self.out.append("\n```\n")
        elif tag == "a" and self._href:
            if "".join(self.out[self._link_start:]).strip():  # skip icon/image-only links
                self.out.append(f" ({self._href})")
            self._href = None
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.skip:
            return
        self.out.append(data if self._pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        raw = "".join(self.out)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _check_public(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("only http(s) URLs can be fetched")
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ToolError(f"could not resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ToolError(f"{host} resolves to a private or local address ({ip}); refusing to fetch it")
    return host


def fetch_url(url: str, max_chars: int = 30000, offset: int = 0) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    _check_public(url)
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
            for _ in range(5):  # follow redirects manually so every hop is checked
                with client.stream("GET", url) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = str(resp.url.join(resp.headers["location"]))
                        _check_public(url)
                        continue
                    if resp.status_code >= 400:
                        raise ToolError(f"HTTP {resp.status_code} for {url}")
                    ctype = resp.headers.get("content-type", "")
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > MAX_DOWNLOAD:
                            break
                    break
            else:
                raise ToolError("too many redirects")
    except httpx.HTTPError as e:
        raise ToolError(f"fetch failed: {type(e).__name__}: {e}")

    text = body.decode(resp.encoding or "utf-8", errors="replace")
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        parser = _TextExtractor(url)
        parser.feed(text)
        title = parser.title.strip()
        text = (f"# {title}\n\n" if title else "") + parser.text()
    elif not any(t in ctype for t in ("text", "json", "xml", "javascript")) and ctype:
        raise ToolError(f"unsupported content type {ctype}")
    # Continuous window, not a middle cut: metadata often sits mid-page (sidebars, release info).
    offset = max(0, min(offset, len(text)))
    chunk = text[offset:offset + max_chars]
    end = offset + len(chunk)
    header = f"[{url} · chars {offset}-{end} of {len(text)}]"
    more = (f"\n\n[... {len(text) - end} more chars. Call web_fetch with the same url and offset={end} "
            "to read on.]") if end < len(text) else ""
    return f"{header}\n\n{chunk}{more}"


# ----- tools -----------------------------------------------------------------

class WebSearch(Tool):
    name = "web_search"
    description = ("Search the web. Returns titles, URLs and snippets. Use for current information, docs, "
                   "error messages and library versions; then web_fetch the most relevant result.")
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "description": f"1-{MAX_RESULTS} (default 8)"},
    }, "required": ["query"]}
    kind = "read"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

    def target(self, args):
        return args.get("query", "")

    def run(self, args, ctx: ToolContext):
        limit = max(1, min(int(args.get("max_results") or 8), MAX_RESULTS))
        results = web_search(args["query"], limit, self.config)
        if not results:
            return f"No results for '{args['query']}'."
        lines = [f"Results for '{args['query']}':"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:300]}")
        return "\n".join(lines)


class WebFetch(Tool):
    name = "web_fetch"
    description = ("Fetch a web page or text file by URL and return its readable text (HTML is converted "
                   "to plain text with headings and links). Use after web_search, or for URLs the user gives.")
    parameters = {"type": "object", "properties": {
        "url": {"type": "string", "description": "http(s) URL"},
        "max_chars": {"type": "integer", "description": "Max characters to return (default 30000)"},
        "offset": {"type": "integer", "description": "Character offset to continue a long page (default 0)"},
    }, "required": ["url"]}
    kind = "web"

    def target(self, args):
        """Permission rules match the domain, e.g. web_fetch(docs.python.org)."""
        url = args.get("url", "")
        host = urlparse(url if "://" in url else "https://" + url).hostname or url
        return host.lower()

    def preview(self, args, ctx):
        return args.get("url", "")

    def run(self, args, ctx: ToolContext):
        return fetch_url(args["url"], max(1000, min(int(args.get("max_chars") or 30000), 60000)),
                         int(args.get("offset") or 0))
