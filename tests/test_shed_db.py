"""shedd registry on the host: rule 3, append-only triggers, versions and rollback (temp DB, no tool code runs)."""

import sqlite3

import pytest
from shed import manifest as mf
from shed.db import DB, RegisterRefused

OK = {"mode": "once", "by": "operator"}


def tool(name="fetch_page", grade="small", uses=(), code="def run(args, shed):\n    return {}\n"):
    m = {"name": name, "grade": grade, "summary": f"{name} summary", "description": "d", "keywords": [name],
         "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "uses": list(uses), "deps": [],
         "permissions": {"network": True, "llm_usd": 0, "files": "none"}, "limits": {}, "examples": [{"args": {}}]}
    return m, {"tool.py": code, "test_tool.py": "def test_x():\n    pass\n", "SKILL.md": f"# {name}"}


def ready(db, m, f):
    h = db.save_draft("b1", m, f)["content_hash"]
    db.record_test_run(h, "tests", 2, 0, "2 passed")
    db.record_review(h, "approve", "ok")
    return h


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "shed.db")


def test_hashes_bind_content_and_permissions():
    m, f = tool()
    assert mf.content_hash(m, f) != mf.content_hash(m, {**f, "tool.py": f["tool.py"] + "#"})
    assert mf.content_hash(m, f) == mf.content_hash({**m, "version": 3, "parent": "x@v1"}, f)
    assert mf.perm_hash(m) != mf.perm_hash({**m, "permissions": {**m["permissions"], "llm_usd": 0.1}})
    assert mf.validate(m, f) == []
    assert mf.validate({**m, "grade": "big"})  # a big tool needs uses


def test_register_needs_test_review_and_approval(db):
    m, f = tool()
    h = db.save_draft("b1", m, f)["content_hash"]
    with pytest.raises(RegisterRefused, match="test run"):
        db.register(h, OK)
    db.record_test_run(h, "tests", 1, 1, "1 failed")
    db.record_review(h, "approve", "ok")
    with pytest.raises(RegisterRefused, match="test run"):
        db.register(h, OK)
    db.record_test_run(h, "tests", 2, 0, "2 passed")
    with pytest.raises(RegisterRefused, match="operator approval"):
        db.register(h, {"mode": "once", "by": "agent"})
    assert db.register(h, OK)["version"] == 1
    assert [t["name"] for t in db.active_tools()] == ["fetch_page"]


def test_big_tool_needs_registered_uses(db):
    hb = ready(db, *tool("house_finder", "big", uses=["fetch_page"]))
    with pytest.raises(RegisterRefused, match="not registered"):
        db.register(hb, OK)
    db.register(ready(db, *tool()), OK)
    assert db.register(hb, OK)["name"] == "house_finder"


def test_append_only(db):
    db.register(ready(db, *tool()), OK)
    for sql in ("UPDATE versions SET grade = 'big'", "DELETE FROM versions", "UPDATE events SET kind = 'x'",
                "DELETE FROM test_runs", "UPDATE reviews SET verdict = 'reject'", "DELETE FROM approvals"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), db.lock:
            db.conn.execute(sql)


def test_versions_and_rollback(db):
    db.register(ready(db, *tool()), OK)
    r2 = db.register(ready(db, *tool(code="def run(args, shed):\n    return {'v': 2}\n")), OK)
    assert r2["version"] == 2 and db.get_version("fetch_page")["parent"] == "fetch_page@v1"
    assert db.rollback("fetch_page", None) == 1
    assert db.get_version("fetch_page")["version"] == 1
    with pytest.raises(ValueError):
        db.rollback("fetch_page", None)
    h = db.history("fetch_page")
    assert [v["active"] for v in h["versions"]] == [True, False]
    assert [e["kind"] for e in h["events"]] == ["registered", "registered", "rollback"]
