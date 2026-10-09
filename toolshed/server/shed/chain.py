"""Chaining: the runtime socket (/run/shed/rt.sock), run tokens and root invocations. Contract section 7."""

import json
import os
import secrets
import socketserver
import threading
import time
from dataclasses import dataclass, field

from shed import runner

MAX_DEPTH = 3
MAX_SUBCALLS = 20
LLM_MAX_TOKENS = 4096
LLM_MODEL = os.environ.get("SHED_TOOL_MODEL") or "deepseek-flash"  # role "tool" (shed.llm in tools)
LLM_EFFORT = os.environ.get("SHED_TOOL_EFFORT") or "low"


@dataclass
class Root:
    invoke_id: str
    grant: str
    deadline: float
    session_id: str = ""
    run_id: str = ""
    subcalls: list = field(default_factory=list)
    n_calls: int = 0
    refused: int = 0  # subcalls refused by the depth, count or deadline limits
    llm_cost: float = 0.0
    llm_by_tool: dict = field(default_factory=dict)
    overlay: dict | None = None  # smoke run of a draft: name -> draft tool, resolved before the registry
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class RunCtx:
    tool: str
    version: int
    uses: list
    llm_usd: float
    depth: int
    root: Root


class Chain:
    """Run tokens and the runtime socket server for one DB (the real one, or a temp one in the selftest)."""

    def __init__(self, db, sock: str = runner.RT_SOCK):
        self.db, self.sock = db, sock
        self.tokens: dict[str, RunCtx] = {}
        self.lock = threading.Lock()
        self.server = None

    # -- tokens --------------------------------------------------------------------------------------
    def _issue(self, ctx: RunCtx) -> str:
        tok = "rt_" + secrets.token_urlsafe(24)
        with self.lock:
            self.tokens[tok] = ctx
        return tok

    def _revoke(self, tok: str) -> None:
        with self.lock:
            self.tokens.pop(tok, None)

    def _ctx(self, tok) -> RunCtx | None:
        with self.lock:
            return self.tokens.get(tok) if isinstance(tok, str) else None

    # -- execution -----------------------------------------------------------------------------------
    def _run(self, tool: dict, args: dict, depth: int, root: Root, timeout_s: float) -> dict:
        perms = tool.get("permissions") or {}
        ctx = RunCtx(tool["name"], tool["version"], list(tool.get("uses") or []),
                     float(perms.get("llm_usd") or 0), depth, root)
        tok = self._issue(ctx)
        try:
            return runner.run_tool(tool["manifest"], tool["files"], args, tok, max(1, int(timeout_s)), sock=self.sock)
        finally:
            self._revoke(tok)

    def invoke(self, name: str, args: dict, grant: str, run_id: str = "", session_id: str = "",
               version: int | None = None) -> dict:
        """A root invocation (POST /invoke). Records an 'invoked' event; the caller applies the 6 KB rule."""
        tool = self.db.get_version(name, version)
        if not tool:
            return {"ok": False, "error": f"no tool {name}", "name": name}
        return self._root(tool, args, grant, run_id, session_id)

    def smoke(self, tool: dict, args: dict, grant: str, overlay: dict) -> dict:
        """A dry root run of a draft (the Chef's smoke run): sub-tools resolve from overlay first; no events."""
        return self._root(tool, args, grant, overlay=overlay)

    def _root(self, tool: dict, args: dict, grant: str, run_id: str = "", session_id: str = "",
              overlay: dict | None = None) -> dict:
        name = tool["name"]
        timeout_s, _ = runner.limits(tool["manifest"])
        invoke_id = ("smoke_" if overlay is not None else "inv_") + secrets.token_hex(6)
        root = Root(invoke_id, grant, time.monotonic() + timeout_s, session_id, run_id, overlay=overlay)
        t0 = time.monotonic()
        out = self._run(tool, args if isinstance(args, dict) else {}, 0, root, timeout_s)
        ms = int((time.monotonic() - t0) * 1000)
        ok = bool(out.get("ok"))
        reply = {"ok": ok, "invoke_id": invoke_id, "name": name, "version": tool["version"], "duration_ms": ms,
                 "subcalls": root.subcalls, "llm_cost_usd": round(root.llm_cost, 6)}
        if root.refused:
            reply["refused_subcalls"] = root.refused
        if ok:
            reply["result"] = out.get("result")
        else:
            reply["error"] = out.get("error") or "failed"
            if out.get("traceback"):
                reply["traceback"] = out["traceback"][-1500:]
        if overlay is not None:
            return reply
        ev = {"invoke_id": invoke_id, "ok": ok, "duration_ms": ms, "llm_cost_usd": root.llm_cost,
              "subcalls": len(root.subcalls), "run_id": run_id, "session_id": session_id}
        if not ok:
            ev["error"] = str(reply["error"])[:500]
        self.db.record_event("invoked", name, tool["version"], ev)
        return reply

    # -- runtime ops ---------------------------------------------------------------------------------
    def handle(self, req: dict) -> dict:
        ctx = self._ctx(req.get("token"))
        if not ctx:
            return {"ok": False, "error": "bad or expired run token"}
        op = req.get("op")
        if op == "call":
            return self._op_call(ctx, req.get("name"), req.get("args") or {})
        if op == "llm":
            return self._op_llm(ctx, req)
        if op == "registry":
            return {"ok": True, "tools": self.db.stats()}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def _op_call(self, ctx: RunCtx, name, args) -> dict:
        root = ctx.root
        if name not in ctx.uses:
            return {"ok": False, "error": f"{ctx.tool} may not call {name!r}: not in its uses {ctx.uses}"}
        with root.lock:
            err = (f"depth limit {MAX_DEPTH} reached" if ctx.depth + 1 > MAX_DEPTH else
                   f"subcall limit {MAX_SUBCALLS} reached" if root.n_calls >= MAX_SUBCALLS else
                   "root deadline reached" if root.deadline - time.monotonic() < 1 else None)
            if err:
                root.refused += 1
                return {"ok": False, "error": err}
            root.n_calls += 1
        left = root.deadline - time.monotonic()
        tool = (root.overlay or {}).get(name) or self.db.get_version(name)
        if not tool:
            return {"ok": False, "error": f"no registered tool {name}"}
        own_t, _ = runner.limits(tool["manifest"])
        t0 = time.monotonic()
        out = self._run(tool, args if isinstance(args, dict) else {}, ctx.depth + 1, root, min(own_t, left))
        ms = int((time.monotonic() - t0) * 1000)
        ok = bool(out.get("ok"))
        with root.lock:
            root.subcalls.append({"name": name, "version": tool["version"], "ok": ok, "duration_ms": ms})
        ev = {"invoke_id": f"{root.invoke_id}.{root.n_calls}", "root_invoke_id": root.invoke_id, "ok": ok,
              "duration_ms": ms, "depth": ctx.depth + 1, "caller": ctx.tool}
        if not ok:
            ev["error"] = str(out.get("error"))[:500]
        if root.overlay is None:
            self.db.record_event("invoked", name, tool["version"], ev)
        if ok:
            return {"ok": True, "result": out.get("result")}
        return {"ok": False, "error": f"{name} failed: {out.get('error')}"}

    def _op_llm(self, ctx: RunCtx, req: dict) -> dict:
        from shed import llm  # lazy: needs the shared client

        root = ctx.root
        if ctx.llm_usd <= 0:
            return {"ok": False, "error": f"{ctx.tool} has no LLM permission (permissions.llm_usd = 0)"}
        with root.lock:
            spent = root.llm_by_tool.get(ctx.tool, 0.0)
        if spent >= ctx.llm_usd:
            return {"ok": False, "error": f"{ctx.tool} used its LLM budget ${ctx.llm_usd} for this run"}
        msgs = req.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return {"ok": False, "error": "messages must be a non-empty list"}
        body = {"model": LLM_MODEL, "messages": msgs, "reasoning_effort": LLM_EFFORT,
                "max_tokens": max(16, min(int(req.get("max_tokens") or 1024), LLM_MAX_TOKENS))}
        if req.get("json"):
            body["response_format"] = {"type": "json_object"}
        try:
            resp = llm.chat(root.grant, body, role="tool", tool=ctx.tool)
        except llm.CapExceeded as e:
            return {"ok": False, "error": f"LLM refused by the meter: cap exceeded {e.info}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"LLM error: {type(e).__name__}: {str(e)[:300]}"}
        cost = float((resp.get("x_meter") or {}).get("cost_usd") or 0)
        with root.lock:
            root.llm_cost += cost
            root.llm_by_tool[ctx.tool] = root.llm_by_tool.get(ctx.tool, 0.0) + cost
        return {"ok": True, "content": llm.text(resp), "cost_usd": cost}

    # -- server --------------------------------------------------------------------------------------
    def serve(self) -> threading.Thread:
        chain = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    line = self.rfile.readline(4 * 1024 * 1024)
                    req = json.loads(line or b"{}")
                    rep = chain.handle(req) if isinstance(req, dict) else {"ok": False, "error": "bad request"}
                except Exception as e:  # noqa: BLE001
                    rep = {"ok": False, "error": f"runtime error: {type(e).__name__}: {e}"}
                try:
                    self.wfile.write(json.dumps(rep, default=str).encode() + b"\n")
                except OSError:
                    pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        os.makedirs(os.path.dirname(self.sock), exist_ok=True)
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        self.server = Server(self.sock, Handler)
        os.chmod(self.sock, 0o666)
        t = threading.Thread(target=self.server.serve_forever, name="rt-sock", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            if os.path.exists(self.sock):
                os.unlink(self.sock)
