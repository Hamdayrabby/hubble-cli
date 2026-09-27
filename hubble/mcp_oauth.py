"""OAuth 2.1 login for remote MCP servers (the MCP authorization spec, 2025-06-18).

Flow, run by `/mcp login <server>`:
  1. Protected-resource metadata: from the 401's `WWW-Authenticate: ... resource_metadata="..."`,
     else `<origin>/.well-known/oauth-protected-resource`. It names the authorization server.
  2. Authorization-server metadata: `/.well-known/oauth-authorization-server` (or OpenID config).
  3. Client: `oauth.client_id` from settings, else dynamic client registration (RFC 7591).
  4. Authorization code + PKCE (S256) in the browser, redirected to a one-shot local listener
     on 127.0.0.1; the `resource` parameter (RFC 8707) binds the token to this server.
  5. Tokens are kept in the OS credential store (plain file ~/.hubble/mcp_tokens.json, private,
     only where no store exists) and refreshed with the refresh token when they expire.
"""

import base64
import hashlib
import json
import os
import re
import secrets as pysecrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from hubble import keystore
from hubble.settings import HOME_DIR

TOKENS_FILE = HOME_DIR / "mcp_tokens.json"


class OAuthError(Exception):
    pass


def _key(server: str) -> str:
    return f"mcp-oauth:{server}"


class TokenStore:
    def __init__(self, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=20, follow_redirects=True)
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ----- persistence -------------------------------------------------

    def _file(self) -> Dict[str, Any]:
        try:
            return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def load(self, server: str) -> Dict[str, Any]:
        if server in self._cache:
            return self._cache[server]
        data: Dict[str, Any] = {}
        raw = keystore.resolve(keystore.REF_PREFIX + _key(server)) if keystore.available() else ""
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
        if not data:
            data = self._file().get(server, {})
        self._cache[server] = data
        return data

    def save(self, server: str, data: Dict[str, Any]):
        self._cache[server] = data
        if keystore.store(_key(server), json.dumps(data)):
            return
        allf = self._file()
        allf[server] = data
        TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKENS_FILE.write_text(json.dumps(allf, indent=2), encoding="utf-8")
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except OSError:
            pass

    def forget(self, server: str):
        self._cache.pop(server, None)
        keystore.delete(keystore.REF_PREFIX + _key(server))
        allf = self._file()
        if allf.pop(server, None) is not None:
            TOKENS_FILE.write_text(json.dumps(allf, indent=2), encoding="utf-8")

    def access_token(self, server: str) -> str:
        data = self.load(server)
        token = data.get("access_token", "")
        if token and data.get("expires_at") and data["expires_at"] < time.time() + 30 and data.get("refresh_token"):
            if self._refresh(server, data):
                token = self.load(server).get("access_token", "")
        return token

    # ----- discovery ---------------------------------------------------

    def _get_json(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self.http.get(url, headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"})
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def discover(self, server_url: str, www_authenticate: str = "") -> Dict[str, Any]:
        parsed = urlparse(server_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        m = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "")
        candidates = [m.group(1)] if m else []
        candidates += [f"{origin}/.well-known/oauth-protected-resource{parsed.path.rstrip('/')}",
                       f"{origin}/.well-known/oauth-protected-resource"]
        prm = next((d for d in (self._get_json(u) for u in candidates) if d), None)
        issuer = ((prm or {}).get("authorization_servers") or [origin])[0].rstrip("/")
        ip = urlparse(issuer)
        iorigin, ipath = f"{ip.scheme}://{ip.netloc}", ip.path.rstrip("/")
        meta = None
        for u in (f"{iorigin}/.well-known/oauth-authorization-server{ipath}",
                  f"{iorigin}/.well-known/openid-configuration{ipath}",
                  f"{issuer}/.well-known/openid-configuration",
                  f"{iorigin}/.well-known/oauth-authorization-server"):
            meta = self._get_json(u)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                break
            meta = None
        if meta is None:
            # Last resort from the 2025-03-26 spec: default endpoints on the server's origin.
            meta = {"authorization_endpoint": f"{origin}/authorize", "token_endpoint": f"{origin}/token",
                    "registration_endpoint": f"{origin}/register"}
        meta["resource"] = (prm or {}).get("resource") or server_url
        meta["scopes_supported"] = (prm or {}).get("scopes_supported") or meta.get("scopes_supported") or []
        return meta

    def register(self, meta: Dict[str, Any], redirect_uri: str) -> Dict[str, Any]:
        endpoint = meta.get("registration_endpoint")
        if not endpoint:
            raise OAuthError("this server does not support automatic client registration; set "
                             "mcp_servers.<name>.oauth.client_id in settings")
        body = {"client_name": "Hubble CLI", "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
        try:
            resp = self.http.post(endpoint, json=body)
        except httpx.HTTPError as e:
            raise OAuthError(f"client registration failed: {e}") from None
        if resp.status_code not in (200, 201):
            raise OAuthError(f"client registration failed: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    # ----- flows -------------------------------------------------------

    def _token_request(self, meta: Dict[str, Any], form: Dict[str, str], client: Dict[str, Any]) -> Dict[str, Any]:
        form = {**form, "client_id": client["client_id"]}
        if meta.get("resource"):
            form["resource"] = meta["resource"]
        auth = None
        if client.get("client_secret"):
            auth = (client["client_id"], client["client_secret"])
        try:
            resp = self.http.post(meta["token_endpoint"], data=form, auth=auth,
                                  headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise OAuthError(f"token request failed: {e}") from None
        if resp.status_code != 200:
            raise OAuthError(f"token request failed: HTTP {resp.status_code} {resp.text[:200]}")
        tok = resp.json()
        if not tok.get("access_token"):
            raise OAuthError("token response had no access_token")
        return tok

    def _store_tokens(self, server: str, tok: Dict[str, Any], meta: Dict[str, Any], client: Dict[str, Any],
                      previous_refresh: str = ""):
        data = {"access_token": tok["access_token"],
                "refresh_token": tok.get("refresh_token") or previous_refresh,
                "expires_at": time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None,
                "token_endpoint": meta["token_endpoint"], "resource": meta.get("resource"),
                "client_id": client["client_id"], "client_secret": client.get("client_secret", "")}
        self.save(server, data)

    def _refresh(self, server: str, data: Dict[str, Any]) -> bool:
        try:
            tok = self._token_request({"token_endpoint": data["token_endpoint"], "resource": data.get("resource")},
                                      {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]},
                                      {"client_id": data["client_id"], "client_secret": data.get("client_secret")})
        except (OAuthError, KeyError):
            return False
        self._store_tokens(server, tok, {"token_endpoint": data["token_endpoint"], "resource": data.get("resource")},
                           {"client_id": data["client_id"], "client_secret": data.get("client_secret", "")},
                           data.get("refresh_token", ""))
        return True

    def refresh(self, config, www_authenticate: str = "") -> bool:
        """Silently renew a stored login after a 401. False when a browser login is needed."""
        data = self.load(config.name)
        return bool(data.get("refresh_token")) and self._refresh(config.name, data)

    def login(self, config, www_authenticate: str = "", open_url: Callable[[str], Any] = webbrowser.open,
              timeout: float = 300, notice: Callable[[str], Any] = print) -> None:
        meta = self.discover(config.url, www_authenticate)
        server = _CallbackServer()
        redirect_uri = f"http://127.0.0.1:{server.port}/callback"
        try:
            oauth = config.oauth or {}
            if oauth.get("client_id"):
                client = {"client_id": oauth["client_id"], "client_secret": oauth.get("client_secret", "")}
            else:
                client = self.register(meta, redirect_uri)
            verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            state = pysecrets.token_urlsafe(16)
            scopes = oauth.get("scopes") or meta.get("scopes_supported") or []
            params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
                      "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                      "resource": meta["resource"]}
            if scopes:
                params["scope"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
            url = meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?") + urlencode(params)
            notice(f"Opening the browser to log in to '{config.name}'. If it does not open, visit:\n{url}")
            open_url(url)
            got = server.wait(timeout)
            if got is None:
                raise OAuthError("timed out waiting for the browser login")
            if got.get("error"):
                raise OAuthError(f"login refused: {got['error']} {got.get('error_description', '')}".strip())
            if got.get("state") != state:
                raise OAuthError("login response had the wrong state (possible CSRF); try again")
            tok = self._token_request(meta, {"grant_type": "authorization_code", "code": got["code"],
                                             "redirect_uri": redirect_uri, "code_verifier": verifier}, client)
            self._store_tokens(config.name, tok, meta, client)
        finally:
            server.close()


class _CallbackServer:
    """One-shot local HTTP listener for the OAuth redirect."""

    def __init__(self):
        result: Dict[str, str] = {}
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in result
                body = ("<h2>Hubble: logged in. You can close this tab.</h2>" if ok else
                        "<h2>Hubble: login failed. Check the terminal.</h2>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if ok or "error" in result:
                    done.set()

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.result, self.done = result, done
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait(self, timeout: float) -> Optional[Dict[str, str]]:
        return self.result if self.done.wait(timeout) else None

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
