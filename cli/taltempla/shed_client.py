"""HTTP client for the toolshed admin API (contract section 5). Stdlib only."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:7700"


class ShedError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"shed {status}: {msg}")
        self.status, self.msg = status, msg


class ShedClient:
    def __init__(self, token_path: Path, url: str | None = None):
        self.url = (url or os.environ.get("TALTEMPLA_SHED_URL") or DEFAULT_URL).rstrip("/")
        self.token_path = Path(token_path)

    def _token(self) -> str:
        try:
            return self.token_path.read_text().strip()
        except OSError:
            raise ShedError(0, f"admin token missing ({self.token_path}); run: make toolshed-up") from None

    def _open(self, method: str, path: str, body: dict | None, timeout: float, auth: bool = True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(raw).get("error", raw)
            except ValueError:
                msg = raw
            raise ShedError(e.code, msg if isinstance(msg, str) else json.dumps(msg)) from None
        except (urllib.error.URLError, OSError) as e:
            raise ShedError(0, f"toolshed unreachable at {self.url}: {getattr(e, 'reason', e)}") from None

    def _json(self, method: str, path: str, body: dict | None = None, timeout: float = 30, auth: bool = True):
        with self._open(method, path, body, timeout, auth) as r:
            return json.loads(r.read() or b"{}")

    def health(self) -> dict:
        return self._json("GET", "/health", timeout=3, auth=False)

    def tools(self) -> list[dict]:
        return self._json("GET", "/tools")["tools"]

    def tool(self, name: str) -> dict | None:
        return next((t for t in self.tools() if t["name"] == name), None)

    def stats(self):
        return self._json("GET", "/stats")

    def lookup(self, query: str, session_id: str) -> dict:
        return self._json("POST", "/lookup", {"query": query, "session_id": session_id})

    def lookup_tool(self, name: str) -> dict:
        return self._json("POST", "/lookup", {"tool": name})

    def invoke(self, name: str, args: dict, grant: str, run_id: str, session_id: str) -> dict:
        body = {"name": name, "args": args, "grant": grant, "run_id": run_id, "session_id": session_id}
        return self._json("POST", "/invoke", body, timeout=900)

    def chef_build(self, body: dict) -> Iterator[dict]:
        """Stream the NDJSON events of one build."""
        with self._open("POST", "/chef/build", body, timeout=900) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    yield {
                        "type": "trace",
                        "phase": "?",
                        "tool": None,
                        "msg": line.decode("utf-8", "replace")[:200],
                    }

    def draft(self, content_hash: str) -> dict:
        return self._json("GET", f"/drafts/{content_hash}")

    def register(self, build_id: str, hashes: list[str], mode: str) -> dict:
        body = {"build_id": build_id, "content_hashes": hashes, "approval": {"mode": mode, "by": "operator"}}
        return self._json("POST", "/register", body, timeout=120)

    def reject(self, build_id: str) -> dict:
        return self._json("POST", "/reject", {"build_id": build_id})

    def rollback(self, name: str, version: int | None = None) -> dict:
        body = {"name": name} | ({"version": version} if version is not None else {})
        return self._json("POST", "/rollback", body)

    def history(self, name: str) -> dict:
        return self._json("GET", "/history?" + urllib.parse.urlencode({"name": name}))
