"""The meter: the only holder of the DeepSeek key and the single point for LLM spend (contract section 4).

Every LLM call goes through Meter.chat(). The CLI agent loop calls it in-process; the toolshed container
(Big Chef, agentic tools via shedd) calls it over a unix socket (serve()). Both take the same path:
grant check -> clamp max_tokens (role clamp, then fit to the budget left, D54) -> reserve an estimate against every
cap in the grant chain and the total cap -> forward upstream -> settle the real cost (peak/off-peak, by UTC request
start) -> one ledger row.

Grants are opaque random tokens in a tree: session -> run -> build | tool_run. A grant inherits run_id,
build_id and tool from its parent. Spend on a grant counts on all its ancestors, so a child can never spend
more than any ancestor allows. Revoking a grant also kills its children.

Contract API: MeterRefused (402), MeterAuthError (401), Meter.grant/revoke/chat/spent/serve/status/balance.

Wait, not refuse: a call that does not fit (or would shrink below half its role clamp) ONLY because of other calls'
in-flight reserves waits for a settle (Condition on the meter lock, <= WAIT_S in-process, WAIT_SOCKET_S on the socket;
a socket caller bounds it with X-Taltempla-Wait, role "tool" waits 0 by default: a tool has its own short deadline),
then fits as before. A revoke wakes it (401); a timeout falls back to the clamp / 402.

Cap top-up (operator): Meter.topup(grant, usd=0.0, seconds=0, deny=False) raises the grant cap (clamped to what the
parent chain and the total cap have left), stores a decision and returns it; the container reads it with POST /v1/topup
(Bearer grant, same 401 rules as chat) -> {"decision": "pending" | "denied" | "raised", "usd", "seconds", "seq"}. The read
does not consume it (a lost reply loses no answer): seq counts the answers, the reader ignores a seq it already used.

Extra helpers (for the CLI and the Makefile):
    Meter.session_grant: str                       made in __init__ (kind "session", cap = caps["session"])
    Meter.status() -> {session_usd, run_usd, tokens, cache_pct, cap_left_usd, saved_usd, total_usd}
    Meter.record_saving(session_id: str | None, tool: str, version: int | None, usd: float) -> None
                                                   None session_id = this session
    Meter.cost_report(session_id: str | None = None) -> dict    None = all sessions; keys:
        by_run / by_role / by_tool / by_build / by_session: [{<key>, calls, hit, miss, out, cost_usd}]
        total: {calls, ok, errors, refused, hit, miss, out, reasoning, cost_usd, cache_pct}
        saved: {total_usd, by_tool: [{tool, version, uses, saved_usd}]}
        balance: {start, end} (only with a session_id)
    Meter.record_balance(which: "start" | "end") -> float | None   balance() into the sessions row
    Meter.close() -> None                          stop the socket server, close the ledger
    report(ledger_path, session_id=None) -> dict   cost_report() without a Meter (read-only)
    format_report(report: dict) -> str             plain-text tables
    load_env(path) -> dict;  from_env(root, session_id=None) -> Meter   (.env + TALTEMPLA_* env vars)
    python -m taltempla.meter [--ledger PATH] [--session ID] [--json]   prints the report (`make cost`)
"""

import argparse
import json
import math
import os
import secrets
import socket
import socketserver
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import llm

PKG = Path(__file__).resolve().parent
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CAPS = {"build": 1.00, "run": 3.00, "session": 6.00, "total": 20.00}
ROLE_MAX_TOKENS = {"main": 16384, "plan": 32768, "tests": 32768, "code": 32768, "security": 16384, "tool": 8192,
                   "waiter": 16384}
# Fit-to-budget clamp (D54): max_tokens shrinks until the reserve fits; it never goes below the floor (402 there).
MIN_TOKENS = {"tool": 1024, "waiter": 2048}  # a cut Waiter reply falls back (G1)
MIN_TOKENS_DEFAULT = 4096
SOCKET_ROLES = {"plan", "tests", "code", "security", "tool"}
KINDS = ("session", "run", "build", "tool_run")
MAX_BODY = 8 << 20
DROP_FIELDS = ("stream", "stream_options", "n", "x_meter")
CHAT_PATHS, TOPUP_PATHS = ("/v1/chat/completions", "/chat/completions"), ("/v1/topup", "/topup")
WAIT_S = 600.0  # max wait for in-flight reserves to settle, in-process callers
WAIT_SOCKET_S = 600.0  # socket callers: wait + the upstream call (600 s) stay under the container timeout (1500 s)
WAIT_SLICE = 5.0  # re-check at least this often (a lost notify never parks a call)


class MeterRefused(Exception):
    status = 402

    def __init__(self, info: dict):
        self.info = info
        super().__init__(f"cap exceeded: {info['cap']} (limit ${info['limit']:.4f}, spent ${info['spent']:.4f}, "
                         f"need ${info['need']:.4f})")


class MeterAuthError(Exception):
    status = 401


def _pos(v) -> float:
    """A finite number > 0, else 0 (bad top-up input falls back to a denial)."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v > 0 else 0.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def is_peak(when: datetime) -> bool:
    """DeepSeek peak: 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday."""
    when = when.astimezone(UTC)
    return when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10)


def price_table(prices: dict, model: str, when: datetime) -> dict:
    p = prices.get(model) or prices.get(DEFAULT_MODEL) or next(iter(prices.values()))
    return p["peak" if is_peak(when) else "offpeak"]


def call_cost(prices: dict, model: str, usage: dict, when: datetime) -> float:
    """USD for one call; prices are USD per 1M tokens."""
    p = price_table(prices, model, when)
    return (usage["hit"] * p["hit"] + usage["miss"] * p["miss"] + usage["out"] * p["out"]) / 1e6


@dataclass(eq=False)
class _Grant:
    token: str
    kind: str
    parent: "_Grant | None"
    cap: float
    label: str
    run_id: str | None
    build_id: str | None
    tool: str | None
    spent: float = 0.0
    reserved: float = 0.0
    revoked: bool = False
    decision: dict | None = None  # the operator's last top-up answer (with seq), read over the socket
    seq: int = 0

    def chain(self):
        g = self
        while g is not None:
            yield g
            g = g.parent


class Meter:
    def __init__(self, *, ledger_path: Path, prices_path: Path, key: str, base_url: str, caps: dict,
                 session_id: str, model: str = DEFAULT_MODEL):
        self._key = key
        self.base_url = base_url.rstrip("/")
        self.caps = {**DEFAULT_CAPS, **{k: float(v) for k, v in (caps or {}).items() if v is not None}}
        self.session_id = session_id
        self.model = model
        self.prices = json.loads(Path(prices_path).read_text())
        self.upstream = llm.post_json  # tests replace this with a fake
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)  # notified on every settle / release / revoke / top-up
        self._grants: dict[str, _Grant] = {}
        self._tok = {"hit": 0, "miss": 0, "out": 0}
        self._session_spent = self._saved = self._total_reserved = 0.0
        self._run: str | None = None
        self._server: socketserver.BaseServer | None = None
        self._sock_path: Path | None = None
        ledger_path = Path(ledger_path)
        ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(ledger_path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.executescript((PKG / "ledger.sql").read_text())
        if "max_tokens" not in {r[1] for r in self._db.execute("PRAGMA table_info(calls)")}:  # a ledger before D54
            self._db.execute("ALTER TABLE calls ADD COLUMN max_tokens INTEGER")
        self._db.execute("INSERT OR IGNORE INTO sessions(id, started) VALUES (?, ?)", (session_id, _now_iso()))
        self._total_spent = self._db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM calls").fetchone()[0]
        self.session_grant = self.grant("session", parent=None, cap_usd=None, label="session")

    # ---- grants -------------------------------------------------------------------------------
    def grant(self, kind: str, *, parent: str | None, cap_usd: float | None, label: str = "",
              run_id: str | None = None, build_id: str | None = None, tool: str | None = None) -> str:
        """New grant. parent=None for a non-session kind means the session grant. cap_usd=None = caps[kind]."""
        if kind not in KINDS:
            raise ValueError(f"unknown grant kind {kind!r}")
        with self._lock:
            p = self._get(parent) if parent else (None if kind == "session" else self._get(self.session_grant))
            cap = float(cap_usd) if cap_usd is not None else self.caps.get(kind, float("inf"))
            run_id = run_id or (p and p.run_id) or (new_id("run") if kind == "run" else None)
            build_id = build_id or (p and p.build_id) or (new_id("build") if kind == "build" else None)
            token = "g-" + secrets.token_urlsafe(24)
            self._grants[token] = _Grant(token, kind, p, cap, label, run_id, build_id, tool or (p and p.tool))
            if kind == "run":
                self._run = token
            return token

    def revoke(self, token: str) -> None:
        with self._lock:
            if g := self._grants.get(token):
                g.revoked = True
                self._cond.notify_all()  # waiters re-check the grant and get 401

    def topup(self, grant: str, usd: float = 0.0, seconds: int = 0, deny: bool = False) -> dict | None:
        """The operator's answer to a cap hit. Raises the cap by usd, clamped so the grant never gets more room than
        its parents and the total cap have left. Nothing applied and no seconds = denied. Returns the stored decision
        (what was applied); unknown/revoked: a no-op, None."""
        with self._lock:
            g = self._grants.get(grant or "")
            if g is None or any(x.revoked for x in g.chain()):
                return None
            g.seq += 1
            usd, seconds = _pos(usd), int(_pos(seconds))
            if deny or not (usd or seconds):
                g.decision = {"decision": "denied", "seq": g.seq}
            else:
                room = min([x.cap - x.spent - x.reserved for x in g.chain() if x is not g]
                           + [self.caps["total"] - self._total_spent - self._total_reserved])
                applied = max(0.0, min(usd, room - (g.cap - g.spent - g.reserved)))
                g.cap += applied
                g.decision = ({"decision": "raised", "usd": round(applied, 6), "seconds": seconds, "seq": g.seq}
                              if applied > 0 or seconds else {"decision": "denied", "seq": g.seq})
            self._cond.notify_all()
            return dict(g.decision)

    def topup_read(self, grant: str) -> dict:
        """The last top-up decision (kept: the reader skips a seq it used), else pending. 401 on a bad grant."""
        with self._lock:
            return dict(self._get(grant).decision or {"decision": "pending"})

    def spent(self, token: str) -> float:
        with self._lock:
            if (g := self._grants.get(token)) is None:
                raise MeterAuthError("unknown grant")
            return g.spent

    def grant_info(self, token: str) -> dict:
        with self._lock:
            g = self._get(token)
            return {"kind": g.kind, "run_id": g.run_id, "build_id": g.build_id, "tool": g.tool, "cap_usd": g.cap,
                    "spent_usd": g.spent, "left_usd": self._left(g)}

    def _get(self, token: str | None) -> _Grant:
        g = self._grants.get(token or "")
        if g is None or any(x.revoked for x in g.chain()):
            raise MeterAuthError("bad grant")
        return g

    def _left(self, g: _Grant) -> float:
        left = min(x.cap - x.spent - x.reserved for x in g.chain())
        return min(left, self.caps["total"] - self._total_spent - self._total_reserved)

    def _reserve(self, g: _Grant, est: float) -> None:
        for x in g.chain():
            if x.spent + x.reserved + est > x.cap:
                raise MeterRefused({"cap": x.kind, "limit": x.cap, "spent": round(x.spent, 6),
                                    "reserved": round(x.reserved, 6), "need": round(est, 6)})
        if self._total_spent + self._total_reserved + est > self.caps["total"]:
            raise MeterRefused({"cap": "total", "limit": self.caps["total"], "spent": round(self._total_spent, 6),
                                "reserved": round(self._total_reserved, 6), "need": round(est, 6)})
        for x in g.chain():
            x.reserved += est
        self._total_reserved += est

    def _release(self, g: _Grant, est: float) -> None:
        for x in g.chain():
            x.reserved = max(0.0, x.reserved - est)
        self._total_reserved = max(0.0, self._total_reserved - est)
        self._cond.notify_all()

    def _must_wait(self, g: _Grant, want: int, in_usd: float, out_usd: float, role: str) -> bool:
        """True when half the role clamp (else the floor) does not fit now but fits without the in-flight reserves:
        only other calls' reserves are in the way, so a settle can free the room."""
        if out_usd <= 0:
            return False
        left = self._left(g)
        free = min(min(x.cap - x.spent for x in g.chain()), self.caps["total"] - self._total_spent)
        for n in (min(want, ROLE_MAX_TOKENS.get(role, ROLE_MAX_TOKENS["tool"]) // 2),
                  min(want, MIN_TOKENS.get(role, MIN_TOKENS_DEFAULT))):
            if in_usd + n * out_usd <= free:
                return in_usd + n * out_usd > left
        return False

    # ---- the call -----------------------------------------------------------------------------
    def _prepare(self, body: dict, role: str) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise TypeError("body needs a 'messages' list")
        if not isinstance(body.get("tools") or [], list):
            raise TypeError("'tools' must be a list")
        body = {k: v for k, v in body.items() if k not in DROP_FIELDS}
        body["model"] = body.get("model") or self.model
        clamp = ROLE_MAX_TOKENS.get(role, ROLE_MAX_TOKENS["tool"])
        try:
            want = int(body.get("max_tokens") or clamp)
        except (TypeError, ValueError):
            want = clamp
        body["max_tokens"] = max(1, min(want, clamp))
        return body

    def chat(self, grant: str, body: dict, *, role: str = "main", tool: str | None = None,
             wait_s: float = WAIT_S) -> dict:
        """[wait] -> fit -> reserve -> forward -> settle. Adds resp["x_meter"] = {cost_usd, grant_spent_usd,
        grant_left_usd, max_tokens, waited_s}."""
        t0 = time.monotonic()
        body = self._prepare(body, role)
        size = len(json.dumps(body["messages"] + (body.get("tools") or []), ensure_ascii=False))

        def price() -> tuple[datetime, float, float]:  # prompt estimate; USD per output token
            when = datetime.now(UTC)
            p = price_table(self.prices, body["model"], when)
            return when, (size / 3) * p["miss"] / 1e6, p["out"] / 1e6

        started, in_usd, out_usd = price()
        with self._lock:
            deadline = t0 + max(0.0, wait_s)
            while True:  # Condition.wait releases the lock; settles, revokes and top-ups notify
                g = self._get(grant)  # revoked while waiting: 401
                rest = deadline - time.monotonic()
                if rest <= 0 or not self._must_wait(g, body["max_tokens"], in_usd, out_usd, role):
                    break
                self._cond.wait(min(rest, WAIT_SLICE))
            if (waited := time.monotonic() - t0) > 0.5:
                started, in_usd, out_usd = price()  # the request starts now (peak / off-peak)
            body["max_tokens"] = self._fit(g, body["max_tokens"], in_usd, out_usd, role)
            est = in_usd + body["max_tokens"] * out_usd
            row = {"ts": started.isoformat(timespec="milliseconds"), "run_id": g.run_id, "build_id": g.build_id,
                   "grant_kind": g.kind, "role": role, "tool": tool or g.tool, "model": body["model"],
                   "max_tokens": body["max_tokens"]}
            try:
                self._reserve(g, est)
            except MeterRefused:
                self._log(row, "refused", 0)
                raise
        try:
            resp = self.upstream(body, url=self.base_url, headers={"Authorization": f"Bearer {self._key}"})
        except BaseException:
            with self._lock:
                self._release(g, est)
                self._log(row, "error", int((time.monotonic() - t0) * 1000))
            raise
        usage = llm.usage_tokens(resp)
        billed = resp.get("model") if resp.get("model") in self.prices else body["model"]
        cost = call_cost(self.prices, billed, usage, started)
        with self._lock:
            self._release(g, est)
            for x in g.chain():
                x.spent += cost
            self._session_spent += cost
            self._total_spent += cost
            for k in self._tok:
                self._tok[k] += usage[k]
            self._log({**row, **usage, "model": resp.get("model") or body["model"], "cost_usd": cost}, "ok",
                      int((time.monotonic() - t0) * 1000))
            resp["x_meter"] = {"cost_usd": round(cost, 6), "grant_spent_usd": round(g.spent, 6),
                               "grant_left_usd": round(self._left(g), 6), "max_tokens": body["max_tokens"],
                               "waited_s": round(waited, 1)}
        return resp

    def _fit(self, g: _Grant, want: int, in_usd: float, out_usd: float, role: str) -> int:
        """Fit-to-budget clamp (D54): the largest max_tokens <= want whose reserve fits the budget left at every
        cap level. Never below min(want, floor); at the floor a reserve that does not fit refuses (402)."""
        left = self._left(g)
        if out_usd <= 0 or in_usd + want * out_usd <= left:
            return want
        floor = min(want, MIN_TOKENS.get(role, MIN_TOKENS_DEFAULT))
        return max(floor, min(want, int((left - in_usd) / out_usd) - 1))

    def _log(self, row: dict, status: str, latency_ms: int) -> None:
        r = {"hit": 0, "miss": 0, "out": 0, "reasoning": 0, "cost_usd": 0.0, "max_tokens": None, **row}
        self._db.execute(
            "INSERT INTO calls(ts, session_id, run_id, build_id, grant_kind, role, tool, model, hit, miss, out,"
            " reasoning, cost_usd, status, latency_ms, max_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["ts"], self.session_id, r["run_id"], r["build_id"], r["grant_kind"], r["role"], r["tool"], r["model"],
             r["hit"], r["miss"], r["out"], r["reasoning"], r["cost_usd"], status, latency_ms, r["max_tokens"]))

    # ---- status, balance, savings, reports ----------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            run = self._grants.get(self._run) if self._run else None
            prompt = self._tok["hit"] + self._tok["miss"]
            return {"session_usd": self._session_spent, "run_usd": run.spent if run else 0.0,
                    "tokens": prompt + self._tok["out"],
                    "cache_pct": round(100 * self._tok["hit"] / prompt, 1) if prompt else 0.0,
                    "cap_left_usd": max(0.0, self._left(run or self._grants[self.session_grant])),
                    "saved_usd": self._saved, "total_usd": self._total_spent}

    def balance(self) -> float | None:
        """GET {base_url}/user/balance -> total USD balance; None on error or without a USD entry."""
        try:
            r = llm.get_json(url=self.base_url, path="/user/balance",
                             headers={"Authorization": f"Bearer {self._key}"}, timeout=15)
            for b in r.get("balance_infos") or []:
                if b.get("currency") == "USD":
                    return float(b["total_balance"])
        except Exception:  # noqa: BLE001 - None on any error, by contract
            return None
        return None

    def record_balance(self, which: str) -> float | None:
        col = {"start": "balance_start", "end": "balance_end"}[which]
        b = self.balance()
        with self._lock:
            self._db.execute(f"UPDATE sessions SET {col} = ? WHERE id = ?", (b, self.session_id))
        return b

    def record_saving(self, session_id: str | None, tool: str, version: int | None, usd: float) -> None:
        sid = session_id or self.session_id
        with self._lock:
            self._db.execute("INSERT INTO savings(session_id, tool, version, saved_usd, ts) VALUES (?,?,?,?,?)",
                             (sid, tool, version, float(usd), _now_iso()))
            if sid == self.session_id:
                self._saved += float(usd)

    def cost_report(self, session_id: str | None = None) -> dict:
        with self._lock:
            return _report(self._db, session_id)

    # ---- unix socket server -------------------------------------------------------------------
    def serve(self, sock_path: Path) -> None:
        """HTTP over a unix socket in a daemon thread. Dir 0700, socket 0600, a stale socket is unlinked."""
        sock_path = Path(sock_path)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(sock_path.parent, 0o700)
        if sock_path.exists() or sock_path.is_symlink():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(sock_path))
                raise RuntimeError(f"another meter is serving on {sock_path}")
            except OSError:
                sock_path.unlink()
            finally:
                probe.close()
        srv = _Server(str(sock_path), _Handler)
        os.chmod(sock_path, 0o600)
        srv.meter = self
        threading.Thread(target=srv.serve_forever, name="meter-sock", daemon=True).start()
        self._server, self._sock_path = srv, sock_path

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            if self._sock_path:
                self._sock_path.unlink(missing_ok=True)
        with self._lock:
            self._db.close()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    meter: Meter


class _Handler(BaseHTTPRequestHandler):
    server_version = "taltempla-meter"

    def log_message(self, *args):  # silent: no stderr noise in the TUI (and client_address is '' on AF_UNIX)
        pass

    def _reply(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        meter = self.server.meter
        if self.path not in CHAT_PATHS + TOPUP_PATHS:
            return self._reply(404, {"error": {"type": "not_found"}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._reply(413, {"error": {"type": "too_large"}})
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._reply(400, {"error": {"type": "bad_json"}})
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        role = self.headers.get("X-Taltempla-Role") or "tool"
        role = role if role in SOCKET_ROLES else "tool"
        try:
            with meter._lock:
                if meter._get(token).kind == "session":  # the container never gets session authority
                    raise MeterAuthError("session grant not allowed on the socket")
            if self.path in TOPUP_PATHS:  # read-only for the container: only the host raises a cap
                return self._reply(200, meter.topup_read(token))
            w = self.headers.get("X-Taltempla-Wait")  # the caller's bound (its deadline); a tool: no wait by default
            w = min(WAIT_SOCKET_S, _pos(w)) if w is not None else 0.0 if role == "tool" else WAIT_SOCKET_S
            resp = meter.chat(token, body, role=role, tool=self.headers.get("X-Taltempla-Tool") or None, wait_s=w)
        except MeterAuthError:
            return self._reply(401, {"error": {"type": "bad_grant"}})
        except MeterRefused as e:
            return self._reply(402, {"error": {"type": "cap_exceeded", **e.info}})
        except llm.LLMError as e:
            return self._reply(502, {"error": {"type": "upstream", "status": e.status, "body": e.body}})
        except (TypeError, ValueError) as e:
            return self._reply(400, {"error": {"type": "bad_request", "msg": str(e)[:200]}})
        except Exception as e:  # noqa: BLE001 - always answer the container
            return self._reply(500, {"error": {"type": "internal", "msg": f"{type(e).__name__}: {e}"[:200]}})
        self._reply(200, resp)


# ---- reports (also usable without a Meter) ------------------------------------------------------
_AGG = "COUNT(*) AS calls, SUM(hit) AS hit, SUM(miss) AS miss, SUM(out) AS out, ROUND(SUM(cost_usd), 6) AS cost_usd"


def _rows(conn: sqlite3.Connection, sql: str, args: list) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _report(conn: sqlite3.Connection, session_id: str | None = None) -> dict:
    flt, args = (" AND session_id = ?", [session_id]) if session_id else ("", [])

    def group(key: str, extra: str = "") -> list[dict]:
        return _rows(conn, f"SELECT {key}, {_AGG} FROM calls WHERE 1=1{flt}{extra} GROUP BY {key} ORDER BY MIN(id)",
                     args)

    total = _rows(conn, "SELECT COUNT(*) AS calls, COALESCE(SUM(status='ok'), 0) AS ok,"
                        " COALESCE(SUM(status='error'), 0) AS errors, COALESCE(SUM(status='refused'), 0) AS refused,"
                        " COALESCE(SUM(hit), 0) AS hit, COALESCE(SUM(miss), 0) AS miss, COALESCE(SUM(out), 0) AS out,"
                        " COALESCE(SUM(reasoning), 0) AS reasoning, ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd"
                        f" FROM calls WHERE 1=1{flt}", args)[0]
    prompt = total["hit"] + total["miss"]
    total["cache_pct"] = round(100 * total["hit"] / prompt, 1) if prompt else 0.0
    saved = _rows(conn, "SELECT tool, version, COUNT(*) AS uses, ROUND(SUM(saved_usd), 6) AS saved_usd FROM savings"
                        f" WHERE 1=1{flt} GROUP BY tool, version ORDER BY MIN(rowid)", args)
    out = {"session_id": session_id,
           "by_run": group("run_id", " AND run_id IS NOT NULL"), "by_role": group("role"),
           "by_tool": group("tool", " AND tool IS NOT NULL"), "by_build": group("build_id", " AND build_id IS NOT NULL"),
           "by_session": group("session_id"), "total": total,
           "saved": {"total_usd": round(sum(r["saved_usd"] for r in saved), 6), "by_tool": saved}}
    if session_id:
        b = conn.execute("SELECT balance_start, balance_end FROM sessions WHERE id = ?", (session_id,)).fetchone()
        out["balance"] = {"start": b[0], "end": b[1]} if b else {"start": None, "end": None}
    return out


def report(ledger_path: Path, session_id: str | None = None) -> dict:
    """cost_report() straight from a ledger file (read-only). A missing file gives an empty report."""
    path = Path(ledger_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if path.exists() else sqlite3.connect(":memory:")
    try:
        if not path.exists():
            conn.executescript((PKG / "ledger.sql").read_text())
        return _report(conn, session_id)
    finally:
        conn.close()


def format_report(r: dict) -> str:
    lines = []

    def table(title: str, key: str, rows: list[dict]) -> None:
        if not rows:
            return
        lines.append(f"\n{title}")
        lines.append(f"  {key:<34} {'calls':>6} {'hit':>9} {'miss':>9} {'out':>8} {'USD':>10}")
        for x in rows:
            lines.append(f"  {x[key]!s:<34} {x['calls']:>6} {x['hit'] or 0:>9} {x['miss'] or 0:>9}"
                         f" {x['out'] or 0:>8} {x['cost_usd'] or 0:>10.6f}")

    t = r["total"]
    lines.append(f"Ledger{' for session ' + r['session_id'] if r.get('session_id') else ' (all sessions)'}: "
                 f"${t['cost_usd']:.6f} in {t['calls']} calls ({t['ok']} ok, {t['errors']} error, "
                 f"{t['refused']} refused) · {t['hit'] + t['miss'] + t['out']} tok · {t['cache_pct']}% cached")
    if not r.get("session_id"):
        table("By session", "session_id", r["by_session"])
    table("By run", "run_id", r["by_run"])
    table("By role", "role", r["by_role"])
    table("By tool", "tool", r["by_tool"])
    table("By build", "build_id", r["by_build"])
    if r["saved"]["by_tool"]:
        lines.append(f"\nSaved by reuse: ${r['saved']['total_usd']:.4f}")
        for x in r["saved"]["by_tool"]:
            lines.append(f"  {x['tool']} v{x['version']}: {x['uses']} uses, ${x['saved_usd']:.4f}")
    b = r.get("balance") or {}
    if b.get("start") is not None and b.get("end") is not None:
        lines.append(f"\nDeepSeek balance: ${b['start']:.4f} -> ${b['end']:.4f} (change ${b['start'] - b['end']:.4f})")
    return "\n".join(lines)


# ---- construction from .env ---------------------------------------------------------------------
def load_env(path: Path) -> dict[str, str]:
    """Simple KEY=VALUE parser (comments, blank lines, `export ` and quotes are handled)."""
    env = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def from_env(root: Path, session_id: str | None = None) -> Meter:
    """Meter with the key from root/.env, the ledger at root/.frank/ledger.db, caps from TALTEMPLA_CAP_*."""
    root = Path(root)
    env = load_env(root / ".env")
    key = env.get("OAI_COMPATIBLE_KEY")
    if not key:
        raise RuntimeError(f"OAI_COMPATIBLE_KEY is missing in {root / '.env'}; ask the operator")
    caps = {k: float(os.environ[f"TALTEMPLA_CAP_{k.upper()}"]) for k in DEFAULT_CAPS
            if os.environ.get(f"TALTEMPLA_CAP_{k.upper()}")}
    return Meter(ledger_path=root / ".frank" / "ledger.db", prices_path=PKG / "prices.json", key=key,
                 base_url=env.get("OAI_COMPATIBLE_BASE_URL") or DEFAULT_BASE_URL, caps=caps,
                 session_id=session_id or new_id("s"), model=os.environ.get("TALTEMPLA_MODEL") or "deepseek-v4-pro")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m taltempla.meter", description="Print the Taltempla spend ledger.")
    ap.add_argument("--ledger", default=".frank/ledger.db")
    ap.add_argument("--session", help="one session id (default: all sessions)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = report(Path(a.ledger), a.session)
    print(json.dumps(r, indent=2) if a.json else format_report(r))


if __name__ == "__main__":
    main()
