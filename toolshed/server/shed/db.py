"""The shedd registry: sqlite3 (WAL, FTS5), one connection guarded by a lock. Contract section 10."""

import json
import secrets
import sqlite3
import threading
from pathlib import Path

from shed import manifest as mf

SCHEMA = Path(__file__).with_name("schema.sql")
LOG_MAX = 20000
APPROVERS = ("operator",)
MODES = ("once", "always")


class RegisterRefused(Exception):
    """Rule 3 failed (no passed test run, no approve review or no operator approval)."""


def _clip(text: str, n: int = LOG_MAX) -> str:
    return text if len(text) <= n else text[: n // 5] + f"\n...[{len(text) - n} chars cut]...\n" + text[-(n * 4 // 5):]


def _j(s):
    return json.loads(s) if s else None


class DB:
    def __init__(self, path: str):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA.read_text())

    # -- helpers -------------------------------------------------------------------------------------
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    @staticmethod
    def _tool(row: dict) -> dict:
        m = json.loads(row["manifest"])
        files = json.loads(row["files"])
        return {"name": row["name"], "grade": row["grade"], "version": row["version"],
                "summary": m.get("summary", ""), "description": m.get("description", ""),
                "keywords": m.get("keywords", []), "uses": m.get("uses", []), "deps": m.get("deps", []),
                "permissions": m.get("permissions", {}), "limits": m.get("limits", {}),
                "input_schema": m.get("input_schema", {}), "output_schema": m.get("output_schema", {}),
                "content_hash": row["content_hash"], "perm_hash": row["perm_hash"], "origin": row["origin"],
                "build_id": row["build_id"], "build_cost_usd": row["build_cost_usd"], "parent": row["parent"],
                "created_at": row["created_at"], "manifest": {**m, "version": row["version"]}, "files": files,
                "skill": files.get("SKILL.md", "")}

    # -- tools ---------------------------------------------------------------------------------------
    def active_tools(self) -> list[dict]:
        rows = self.q("SELECT v.* FROM versions v JOIN active a ON a.name = v.name AND a.version = v.version "
                      "ORDER BY v.grade = 'small', v.name")
        out = []
        for r in rows:
            t = self._tool(r)
            del t["files"], t["manifest"]
            out.append(t)
        return out

    def get_version(self, name: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self.one("SELECT v.* FROM versions v JOIN active a ON a.name = v.name AND a.version = v.version "
                           "WHERE v.name = ?", (name,))
        else:
            row = self.one("SELECT * FROM versions WHERE name = ? AND version = ?", (name, version))
        return self._tool(row) if row else None

    def _fts_refresh(self, name: str) -> None:
        self.conn.execute("DELETE FROM tools_fts WHERE name = ?", (name,))
        t = self.get_version(name)
        if t:
            self.conn.execute("INSERT INTO tools_fts(name, summary, keywords, description, skill) VALUES (?,?,?,?,?)",
                              (t["name"].replace("_", " ") + " " + t["name"], t["summary"],
                               " ".join(t["keywords"]), t["description"], t["skill"]))

    # -- drafts, tests, reviews ----------------------------------------------------------------------
    def save_draft(self, build_id: str, manifest: dict, files: dict[str, str], cost_usd: float = 0.0) -> dict:
        ch, ph = mf.content_hash(manifest, files), mf.perm_hash(manifest)
        files = {k: files.get(k, "") for k in mf.FILES}
        self.x("INSERT INTO drafts(content_hash, build_id, name, perm_hash, manifest, files, cost_usd) "
               "VALUES (?,?,?,?,?,?,?) ON CONFLICT(content_hash) DO UPDATE SET build_id = excluded.build_id, "
               "cost_usd = excluded.cost_usd",
               (ch, build_id, manifest.get("name", ""), ph, json.dumps(manifest), json.dumps(files), cost_usd))
        return {"content_hash": ch, "perm_hash": ph}

    def get_draft(self, content_hash: str) -> dict | None:
        row = self.one("SELECT * FROM drafts WHERE content_hash = ?", (content_hash,))
        if not row:
            return None
        runs = self.q("SELECT id, kind, passed, failed, log, ts FROM test_runs WHERE content_hash = ? ORDER BY ts, rowid",
                      (content_hash,))
        review = self.one("SELECT verdict, report, ts FROM reviews WHERE content_hash = ? ORDER BY id DESC LIMIT 1",
                          (content_hash,))
        return {"content_hash": content_hash, "perm_hash": row["perm_hash"], "build_id": row["build_id"],
                "name": row["name"], "cost_usd": row["cost_usd"], "manifest": json.loads(row["manifest"]),
                "files": json.loads(row["files"]), "test_runs": runs, "review": review}

    def record_test_run(self, content_hash: str, kind: str, passed: int, failed: int, log: str) -> str:
        rid = "tr_" + secrets.token_hex(6)
        self.x("INSERT INTO test_runs(id, content_hash, kind, passed, failed, log) VALUES (?,?,?,?,?,?)",
               (rid, content_hash, kind, int(passed), int(failed), _clip(log or "")))
        return rid

    def record_review(self, content_hash: str, verdict: str, report: str) -> None:
        if verdict not in ("approve", "reject"):
            raise ValueError("verdict must be approve or reject")
        self.x("INSERT INTO reviews(content_hash, verdict, report) VALUES (?,?,?)",
               (content_hash, verdict, _clip(report or "", 8000)))

    def record_event(self, kind: str, tool: str | None, version: int | None, data: dict) -> None:
        self.x("INSERT INTO events(kind, tool, version, data) VALUES (?,?,?,?)",
               (kind, tool, version, json.dumps(data, default=str)))

    # -- builds --------------------------------------------------------------------------------------
    def start_build(self, build_id, session_id, task, need, lookup_id, repair_of) -> None:
        self.x("INSERT OR IGNORE INTO builds(build_id, session_id, task, need, lookup_id, repair_of) VALUES (?,?,?,?,?,?)",
               (build_id, session_id, task, need, lookup_id, json.dumps(repair_of) if repair_of else None))

    def add_trace(self, build_id: str, event: dict) -> None:
        self.x("INSERT INTO builds_log(build_id, event) VALUES (?,?)", (build_id, json.dumps(event, default=str)))

    def finish_build(self, build_id: str, status: str, cost_usd: float, handoff: dict | None) -> None:
        self.x("UPDATE builds SET status = ?, cost_usd = ?, handoff = ?, finished = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
               "WHERE build_id = ?", (status, cost_usd, json.dumps(handoff) if handoff else None, build_id))

    def set_build_status(self, build_id: str, status: str) -> None:
        self.x("UPDATE builds SET status = ? WHERE build_id = ?", (status, build_id))

    def get_build(self, build_id: str) -> dict | None:
        b = self.one("SELECT * FROM builds WHERE build_id = ?", (build_id,))
        if b:
            b["handoff"], b["repair_of"] = _j(b["handoff"]), _j(b["repair_of"])
        return b

    # -- lookups and invocations ---------------------------------------------------------------------
    def save_lookup(self, session_id: str, query: str, fit: str, rows: list) -> str:
        lid = "lk_" + secrets.token_hex(6)
        self.x("INSERT INTO lookups(id, session_id, query, fit, rows) VALUES (?,?,?,?,?)",
               (lid, session_id, query, fit, json.dumps(rows)))
        return lid

    def get_lookup(self, lookup_id: str) -> dict | None:
        r = self.one("SELECT id, session_id, query, fit, rows FROM lookups WHERE id = ?", (lookup_id,))
        if r:
            r["rows"] = json.loads(r["rows"])
        return r

    def get_invocation(self, invoke_id: str) -> dict | None:
        r = self.one("SELECT tool, version, data FROM events WHERE kind = 'invoked' "
                     "AND json_extract(data, '$.invoke_id') = ?", (invoke_id,))
        if not r:
            return None
        d = json.loads(r["data"])
        return {"invoke_id": invoke_id, "tool": r["tool"], "version": r["version"], "ok": bool(d.get("ok")),
                "error": d.get("error"), "session_id": d.get("session_id") or ""}

    # -- register / rollback -------------------------------------------------------------------------
    def check_register(self, content_hash: str, approval: dict | None) -> tuple[dict | None, list[str]]:
        d = self.get_draft(content_hash)
        if not d:
            return None, [f"{content_hash[:12]}: no such draft"]
        why = [f"{d['name']}: {e}" for e in mf.validate(d["manifest"], d["files"])]
        if not any(r["kind"] == "tests" and r["passed"] > 0 and r["failed"] == 0 for r in d["test_runs"]):
            why.append(f"{d['name']}: no passed test run for this content hash")
        if not d["review"] or d["review"]["verdict"] != "approve":
            why.append(f"{d['name']}: no approve review for this content hash")
        if not approval or approval.get("by") not in APPROVERS or approval.get("mode") not in MODES:
            why.append(f"{d['name']}: no operator approval (approval.by=operator, mode once|always)")
        return d, why

    def register(self, content_hash: str, approval: dict) -> dict:
        with self.lock:
            done = self.one("SELECT name, version, content_hash, perm_hash FROM versions WHERE content_hash = ?",
                            (content_hash,))
            if done:
                return done
            d, why = self.check_register(content_hash, approval)
            m = d["manifest"] if d else {}
            missing = [u for u in m.get("uses", []) if not self.one("SELECT 1 FROM active WHERE name = ?", (u,))]
            if missing:
                why.append(f"{m.get('name')}: uses tools that are not registered: {missing}")
            if why:
                raise RegisterRefused("; ".join(why))
            name = m["name"]
            prev = self.one("SELECT version FROM active WHERE name = ?", (name,))
            last = self.one("SELECT MAX(version) AS v FROM versions WHERE name = ?", (name,))["v"] or 0
            version = last + 1
            parent = m.get("parent") or (f"{name}@v{prev['version']}" if prev else None)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO versions(name, version, grade, content_hash, perm_hash, manifest, files, parent, origin, "
                    "build_id, build_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (name, version, m["grade"], content_hash, d["perm_hash"], json.dumps(m), json.dumps(d["files"]),
                     parent, "chef", d["build_id"], d["cost_usd"]))
                self.conn.execute("INSERT INTO approvals(content_hash, build_id, mode, by) VALUES (?,?,?,?)",
                                  (content_hash, d["build_id"], approval["mode"], approval["by"]))
                self.conn.execute("INSERT INTO active(name, version) VALUES (?, ?) "
                                  "ON CONFLICT(name) DO UPDATE SET version = excluded.version", (name, version))
                self._fts_refresh(name)
                self.conn.execute("INSERT INTO events(kind, tool, version, data) VALUES ('registered', ?, ?, ?)",
                                  (name, version, json.dumps({"content_hash": content_hash, "build_id": d["build_id"],
                                                              "parent": parent, "mode": approval["mode"]})))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return {"name": name, "version": version, "content_hash": content_hash, "perm_hash": d["perm_hash"]}

    def rollback(self, name: str, version: int | None) -> int:
        with self.lock:
            cur = self.one("SELECT version FROM active WHERE name = ?", (name,))
            if not cur:
                raise KeyError(f"no active tool {name}")
            if version is None:
                prev = self.one("SELECT MAX(version) AS v FROM versions WHERE name = ? AND version < ?",
                                (name, cur["version"]))["v"]
                if not prev:
                    raise ValueError(f"{name} has no version before v{cur['version']}")
                version = prev
            if not self.one("SELECT 1 FROM versions WHERE name = ? AND version = ?", (name, version)):
                raise KeyError(f"no version {name}@v{version}")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("UPDATE active SET version = ? WHERE name = ?", (version, name))
                self._fts_refresh(name)
                self.conn.execute("INSERT INTO events(kind, tool, version, data) VALUES ('rollback', ?, ?, ?)",
                                  (name, version, json.dumps({"from": cur["version"], "to": version})))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return version

    # -- reports -------------------------------------------------------------------------------------
    def stats(self) -> list[dict]:
        rows = self.q(
            "SELECT v.name, v.version, v.grade, v.build_cost_usd, "
            " COUNT(e.id) AS invocations, "
            " COALESCE(SUM(json_extract(e.data, '$.ok') = 0), 0) AS failures, "
            " COALESCE(AVG(json_extract(e.data, '$.duration_ms')), 0) AS avg_ms, "
            " COALESCE(SUM(json_extract(e.data, '$.llm_cost_usd')), 0) AS llm_cost_usd "
            "FROM active a JOIN versions v ON v.name = a.name AND v.version = a.version "
            "LEFT JOIN events e ON e.kind = 'invoked' AND e.tool = v.name AND e.version = v.version "
            "GROUP BY v.name ORDER BY v.grade = 'small', v.name")
        for r in rows:
            r["avg_ms"] = int(r["avg_ms"])
            r["llm_cost_usd"] = round(r["llm_cost_usd"], 6)
        return rows

    def history(self, name: str) -> dict:
        versions = self.q("SELECT name, version, grade, content_hash, perm_hash, parent, origin, build_id, "
                          "build_cost_usd, created_at, json_extract(manifest, '$.summary') AS summary "
                          "FROM versions WHERE name = ? ORDER BY version", (name,))
        act = self.one("SELECT version FROM active WHERE name = ?", (name,))
        for v in versions:
            v["active"] = bool(act and act["version"] == v["version"])
        events = self.q("SELECT kind, version, data, ts FROM events WHERE tool = ? ORDER BY id DESC LIMIT 200", (name,))
        for e in events:
            e["data"] = json.loads(e["data"])
        return {"versions": versions, "events": events[::-1]}

    def record_package(self, name: str, ok: bool, log: str) -> None:
        self.x("INSERT INTO packages(name, ok, log) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET ok = excluded.ok, "
               "log = excluded.log, ts = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')", (name, int(ok), _clip(log, 4000)))

    def packages(self) -> list[dict]:
        return self.q("SELECT name, ok, ts FROM packages ORDER BY name")
