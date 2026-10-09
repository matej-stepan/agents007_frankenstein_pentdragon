"""shed_sdk: what tool code sees. Shed talks to the runtime socket (/run/shed/rt.sock) with SHED_RUN_TOKEN."""

import json as _json
import os
import socket
import sys
from pathlib import Path

__all__ = ["Shed", "ShedError"]


class ShedError(Exception):
    """A refused or failed shed operation (call outside uses, limit, LLM refused, sub-tool failed)."""


class Shed:
    work: Path = Path("/work")
    out_dir: Path = Path("/work/out")

    def __init__(self, sock: str | None = None, token: str | None = None):
        self._sock = sock or os.environ.get("SHED_SOCK", "/run/shed/rt.sock")
        self._token = token if token is not None else os.environ.get("SHED_RUN_TOKEN", "")

    def _rpc(self, req: dict) -> dict:
        req = {**req, "token": self._token}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(self._sock)
                s.sendall(_json.dumps(req).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise ShedError(f"runtime socket: {e}") from None
        try:
            rep = _json.loads(buf or b"{}")
        except ValueError:
            raise ShedError("runtime socket: bad reply") from None
        if not rep.get("ok"):
            raise ShedError(rep.get("error") or "runtime socket: no reply")
        return rep

    def call(self, name: str, args: dict) -> dict:
        """Call another registered tool. Only names in this tool's manifest "uses" are allowed."""
        return self._rpc({"op": "call", "name": name, "args": args})["result"]

    def llm(self, prompt: str | list[dict], *, max_tokens: int = 1024, json: bool = False) -> str:
        """One LLM completion through the meter (charged to the root run). Needs permissions.llm_usd > 0."""
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        return self._rpc({"op": "llm", "messages": messages, "max_tokens": max_tokens, "json": json})["content"]

    def registry(self) -> list[dict]:
        """Read-only per-tool stats rows (name, version, invocations, failures, avg_ms, costs)."""
        return self._rpc({"op": "registry"})["tools"]

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)
