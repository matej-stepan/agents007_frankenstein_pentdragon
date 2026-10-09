"""shedd admin API (HTTP JSON, ThreadingHTTPServer). Contract section 5."""

import hmac
import json
import os
import secrets
import signal
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shed import lookup as lk
from shed import pkgindex, runner
from shed.chain import Chain
from shed.db import DB, RegisterRefused

DB_PATH = os.environ.get("SHED_DB", "/data/shed.db")
WORK = Path(os.environ.get("SHED_WORK", "/work"))
METER_SOCK = "/run/meter/meter.sock"
INLINE_MAX = 6144
PREVIEW = 1500


class HTTPError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class Shedd:
    def __init__(self, db: DB, chain: Chain, admin_token: str):
        self.db, self.chain, self.admin_token = db, chain, admin_token

    # -- routes --------------------------------------------------------------------------------------
    def health(self, _):
        return {"ok": True, "tools": len(self.db.q("SELECT name FROM active")), "meter": os.path.exists(METER_SOCK)}

    def tools(self, _):
        stats = {s["name"]: s for s in self.db.stats()}
        keys = ("name", "grade", "version", "summary", "uses", "permissions", "content_hash", "perm_hash", "origin",
                "build_cost_usd", "created_at")
        rows = []
        for t in self.db.active_tools():
            s = stats.get(t["name"], {})
            rows.append({**{k: t[k] for k in keys}, "invocations": s.get("invocations", 0),
                         "failures": s.get("failures", 0)})
        return {"tools": rows}

    def lookup(self, body):
        if body.get("tool"):
            d = lk.tool_detail(self.db, body["tool"])
            if not d:
                raise HTTPError(404, f"no tool {body['tool']}")
            return d
        if not isinstance(body.get("query"), str) or not body["query"].strip():
            raise HTTPError(400, "query or tool is required")
        return lk.lookup(self.db, body["query"], body.get("session_id") or "")

    def invoke(self, body):
        name = body.get("name")
        if not self.db.get_version(name or ""):
            raise HTTPError(404, f"no tool {name}")
        r = self.chain.invoke(name, body.get("args") or {}, body.get("grant") or "", body.get("run_id") or "",
                              body.get("session_id") or "")
        if r.get("ok"):
            text = json.dumps(r["result"], ensure_ascii=False, default=str)
            if len(text.encode()) > INLINE_MAX:
                out = WORK / "out" / f"{name}-{r['invoke_id']}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(r["result"], ensure_ascii=False, indent=1, default=str))
                out.chmod(0o644)
                del r["result"]
                r["out_path"] = f"workspace/out/{out.name}"
                r["preview"] = text[:PREVIEW]
        return r

    def check_build(self, body) -> dict | None:
        """Gap rule (contract section 5). Returns the repair_of for BuildCtx {tool, invoke_id, problem, args, error}."""
        repair = body.get("repair_of")
        if repair:
            if not isinstance(repair, dict):
                raise HTTPError(400, "repair_of must be an object {tool, invoke_id, problem?}")
            inv = self.db.get_invocation(repair.get("invoke_id") or "")
            if not inv or inv["tool"] != repair.get("tool"):
                raise HTTPError(400, "repair_of must name an invocation of that tool")
            problem = str(repair.get("problem") or "").strip()
            if inv["ok"]:
                if not problem:
                    raise HTTPError(400, "repair_of names an ok invocation: improve mode needs a problem")
                if inv["session_id"] != (body.get("session_id") or ""):
                    raise HTTPError(400, "improve mode: the invocation is not from this session")
            elif str(inv["error"] or "").startswith("ValueError"):
                raise HTTPError(400, "the invocation failed on its arguments (ValueError): fix the args, no repair")
            repair = {"tool": inv["tool"], "invoke_id": inv["invoke_id"], "problem": problem,
                      "args": repair.get("args"), "error": inv["error"]}
        else:
            lu = self.db.get_lookup(body.get("lookup_id") or "")
            if not lu:
                raise HTTPError(400, "gap rule: lookup_id is unknown; call lookup first")
            if lu["session_id"] != (body.get("session_id") or ""):
                raise HTTPError(400, "gap rule: lookup_id is from another session")
            if lu["fit"] not in ("none", "partial"):
                raise HTTPError(400, f"gap rule: lookup fit is {lu['fit']}; use the existing tool")
        for k in ("task", "need", "build_id", "grant"):
            if not body.get(k):
                raise HTTPError(400, f"{k} is required")
        return repair or None

    def chef_build(self, body, stream):
        repair = self.check_build(body)
        from shed.chef import orchestrator  # lazy: written by the Chef owner

        build_id = body["build_id"]
        self.db.start_build(build_id, body.get("session_id"), body["task"], body["need"], body.get("lookup_id"), repair)
        write = stream()

        def emit(event: dict) -> None:  # stream the line and store it (the orchestrator relies on this)
            self.db.add_trace(build_id, event)
            write(event)

        ctx = orchestrator.BuildCtx(db=self.db, grant=body["grant"], task=body["task"], need=body["need"],
                                    lookup_id=body.get("lookup_id"), session_id=body.get("session_id") or "",
                                    build_id=build_id, repair_of=repair, chain=self.chain)
        try:
            handoff = orchestrator.build(ctx, emit)
            b = self.db.get_build(build_id)
            if b and b["status"] == "running":
                ok = isinstance(handoff, dict) and handoff.get("type", "handoff") == "handoff" and handoff.get("tree")
                self.db.finish_build(build_id, "handoff" if ok else "failed",
                                     float((handoff or {}).get("cost_usd") or 0), handoff if ok else None)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            emit({"type": "failed", "reason": f"{type(e).__name__}: {e}"[:500], "cost_usd": 0.0})
            self.db.finish_build(build_id, "failed", 0.0, None)

    def draft(self, content_hash):
        d = self.db.get_draft(content_hash)
        if not d:
            raise HTTPError(404, "no such draft")
        return d

    def register(self, body):
        build_id, hashes, approval = body.get("build_id"), body.get("content_hashes") or [], body.get("approval")
        if not build_id or not isinstance(hashes, list) or not hashes:
            raise HTTPError(400, "build_id and content_hashes are required")
        problems, drafts = [], {}
        for h in hashes:
            d, why = self.db.check_register(h, approval)
            if d and d["build_id"] != build_id:
                why.append(f"{d['name']}: draft is not from build {build_id}")
            problems += why
            drafts[h] = d
        if problems:
            raise HTTPError(409, "; ".join(problems))
        done, pending = [], list(hashes)
        while pending:  # small tools first: register a draft once everything it uses is active
            ready = [h for h in pending if all(self.db.get_version(u) for u in drafts[h]["manifest"].get("uses", []))]
            if not ready:
                raise HTTPError(409, f"uses not satisfied for {[drafts[h]['name'] for h in pending]}")
            for h in ready:
                try:
                    done.append(self.db.register(h, approval))
                except RegisterRefused as e:
                    raise HTTPError(409, str(e)) from None
                pending.remove(h)
        self.db.set_build_status(build_id, "registered")
        return {"registered": done}

    def reject(self, body):
        if not body.get("build_id"):
            raise HTTPError(400, "build_id is required")
        self.db.set_build_status(body["build_id"], "rejected")
        self.db.record_event("rejected", None, None, {"build_id": body["build_id"]})
        return {"ok": True}

    def rollback(self, body):
        try:
            v = self.db.rollback(body.get("name") or "", body.get("version"))
        except (KeyError, ValueError) as e:
            raise HTTPError(404 if isinstance(e, KeyError) else 409, str(e).strip("'\"")) from None
        return {"name": body["name"], "active_version": v}

    def history(self, query):
        name = (query.get("name") or [""])[0]
        if not name:
            raise HTTPError(400, "name is required")
        return self.db.history(name)

    def stats(self, _):
        return {"tools": self.db.stats()}


def make_handler(app: Shedd):
    class Handler(BaseHTTPRequestHandler):
        server_version = "shedd/0.1"

        def log_message(self, fmt, *args):
            print(f"[shedd] {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)

        def _send(self, status: int, obj) -> None:
            data = json.dumps(obj, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            return bool(app.admin_token) and hmac.compare_digest(got.encode(), f"Bearer {app.admin_token}".encode())

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                raise HTTPError(400, "body is not JSON") from None
            if not isinstance(body, dict):
                raise HTTPError(400, "body must be a JSON object")
            return body

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            alive = [True]

            def write(event: dict) -> None:
                if not alive[0]:
                    return
                try:
                    self.wfile.write(json.dumps(event, default=str).encode() + b"\n")
                    self.wfile.flush()
                except OSError:
                    alive[0] = False  # the client left; the build continues and keeps its drafts

            return write

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            path, query = url.path.rstrip("/") or "/", parse_qs(url.query)
            try:
                if method == "GET" and path == "/health":
                    return self._send(200, app.health(None))
                if not self._authed():
                    return self._send(401, {"error": "admin token required"})
                if method == "GET":
                    if path == "/tools":
                        return self._send(200, app.tools(None))
                    if path == "/stats":
                        return self._send(200, app.stats(None))
                    if path == "/history":
                        return self._send(200, app.history(query))
                    if path.startswith("/drafts/"):
                        return self._send(200, app.draft(path.split("/", 2)[2]))
                elif method == "POST":
                    body = self._body()
                    if path == "/chef/build":
                        return app.chef_build(body, self._stream)
                    routes = {"/lookup": app.lookup, "/invoke": app.invoke, "/register": app.register,
                              "/reject": app.reject, "/rollback": app.rollback}
                    if path in routes:
                        return self._send(200, routes[path](body))
                self._send(404, {"error": f"no route {method} {path}"})
            except HTTPError as e:
                self._send(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                try:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                except OSError:
                    pass

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def prepare_dirs() -> None:
    os.umask(0o022)
    data = Path(DB_PATH).parent
    data.mkdir(parents=True, exist_ok=True)
    if str(data) == "/data":
        data.chmod(0o700)
    for sub in ("in", "out"):
        p = WORK / sub
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o1777)


def main() -> None:
    token = os.environ.pop("SHED_ADMIN_TOKEN", "")
    if not token:
        token = secrets.token_urlsafe(32)
        print("[shedd] WARNING: no SHED_ADMIN_TOKEN; the admin API is locked (random token).", flush=True)
    prepare_dirs()
    db = DB(DB_PATH)
    chain = Chain(db, sock=os.environ.get("SHED_RT_SOCK", runner.RT_SOCK))
    chain.serve()
    pkgindex.init(db)
    pkgindex.reinstall_recorded(db)
    host, port = os.environ.get("SHED_HOST", "0.0.0.0"), int(os.environ.get("SHED_PORT", "7700"))
    server = ThreadingHTTPServer((host, port), make_handler(Shedd(db, chain, token)))
    server.daemon_threads = True
    print(f"[shedd] listening on {host}:{port}; db {DB_PATH}; tools {len(db.active_tools())}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 ignores SIGTERM without a handler
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        chain.stop()
