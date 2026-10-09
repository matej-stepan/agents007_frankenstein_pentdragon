"""shedd: /chef/build gap rule for repair and improve; /invoke runs only big tools (temp DB, no tool runs)."""

import pytest
from shed.app import HTTPError, Shedd
from shed.db import DB

BASE = {"task": "t", "need": "n", "build_id": "b1", "grant": "g", "session_id": "s1", "lookup_id": ""}


@pytest.fixture
def app(tmp_path):
    db = DB(tmp_path / "shed.db")
    for iid, ok, err, sess in [("inv_ok", True, None, "s1"), ("inv_old", True, None, "s0"),
                               ("inv_arg", False, "ValueError: bad district", "s1"),
                               ("inv_bug", False, "KeyError: 'price'", "s0")]:
        db.record_event("invoked", "car_search", 1, {"invoke_id": iid, "ok": ok, "session_id": sess}
                        | ({"error": err} if err else {}))
    return Shedd(db, chain=None, admin_token="x")


def check(app, **rep):
    return app.check_build(BASE | {"repair_of": rep})


@pytest.mark.parametrize("rep", [
    {"tool": "car_search", "invoke_id": "inv_ok"},                                 # ok, no problem
    {"tool": "car_search", "invoke_id": "inv_old", "problem": "price is null"},    # ok, another session
    {"tool": "car_search", "invoke_id": "inv_arg", "problem": "x"},                # argument error
    {"tool": "other", "invoke_id": "inv_bug"},                                     # another tool
    {"tool": "car_search", "invoke_id": "nope"},
])
def test_repair_refused(app, rep):
    with pytest.raises(HTTPError) as e:
        check(app, **rep)
    assert e.value.status == 400


def test_improve_and_repair_accepted(app):
    r = check(app, tool="car_search", invoke_id="inv_ok", problem=" price is null ", args={"q": "octavia"})
    assert r == {"tool": "car_search", "invoke_id": "inv_ok", "problem": "price is null", "args": {"q": "octavia"},
                 "error": None}
    r = check(app, tool="car_search", invoke_id="inv_bug")  # a real failure: any session, no problem needed
    assert r["error"] == "KeyError: 'price'" and r["problem"] == "" and r["args"] is None


class FakeChain:
    def __init__(self):
        self.names = []

    def invoke(self, name, args, grant, run_id, session_id):
        self.names.append(name)
        return {"ok": True, "invoke_id": "inv_x", "name": name, "version": 1, "result": {"results": []}}


def register(db, name, grade, uses=()):
    m = {"name": name, "grade": grade, "summary": name, "description": name, "keywords": [name],
         "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "uses": list(uses), "deps": [],
         "permissions": {"network": True, "llm_usd": 0, "files": "none"}, "limits": {}, "examples": [{"args": {}}]}
    files = {"tool.py": "def run(args, shed):\n    return {}\n", "test_tool.py": "def test_x():\n    pass\n",
             "SKILL.md": name}
    h = db.save_draft("b", m, files)["content_hash"]
    db.record_test_run(h, "tests", 1, 0, "")
    db.record_review(h, "approve", "")
    db.register(h, {"mode": "once", "by": "operator"})


def test_invoke_runs_only_big_tools(tmp_path):
    db, chain = DB(tmp_path / "shed.db"), FakeChain()
    register(db, "download_file", "small")
    register(db, "image_fetcher", "big", uses=["download_file"])
    app = Shedd(db, chain=chain, admin_token="x")
    with pytest.raises(HTTPError) as e:
        app.invoke({"name": "download_file", "args": {"url": "https://example.com/a.jpg"}})
    assert e.value.status == 400 and "small building block" in str(e.value) and chain.names == []
    assert app.invoke({"name": "image_fetcher", "args": {}})["ok"] and chain.names == ["image_fetcher"]
    assert "input_schema" not in app.lookup({"tool": "download_file"})  # a note, not the schema
    assert app.lookup({"tool": "image_fetcher"})["uses"] == ["download_file"]


def test_chef_build_options_are_lenient(tmp_path, monkeypatch):
    from shed.chef import orchestrator
    db = DB(tmp_path / "shed.db")
    seen = []
    monkeypatch.setattr(orchestrator, "build", lambda ctx, emit: seen.append(ctx) or {"type": "failed"})
    app = Shedd(db, chain=None, admin_token="x")
    for i, (opts, adv) in enumerate([("junk", 42), ({"effort": "max", "cap_seconds": "120"}, "a" * 3000)]):
        lu = db.save_lookup("s1", "q", "none", [])
        app.chef_build(BASE | {"lookup_id": lu, "build_id": f"b{i}", "options": opts, "advice": adv},
                       lambda: (lambda e: None))
    assert (seen[0].effort, seen[0].cap_seconds, seen[0].advice) == (None, None, "")
    assert (seen[1].effort, seen[1].cap_seconds, len(seen[1].advice)) == ("max", "120", 2000)


def test_resume_of_needs_a_failed_build_with_a_plan_checkpoint(tmp_path, monkeypatch):
    from shed.chef import orchestrator
    db = DB(tmp_path / "shed.db")
    for bid, sess, status, cp in [("bf", "s1", "failed", True), ("bn", "s1", "failed", False),
                                  ("bo", "s0", "failed", True), ("br", "s1", "running", True)]:
        db.start_build(bid, sess, "t", "n", None, None)
        db.set_build_status(bid, status)
        if cp:
            db.add_trace(bid, {"type": "checkpoint", "stage": "plan", "plan": {}})
    app = Shedd(db, chain=None, admin_token="x")
    assert app.check_resume(BASE | {"resume_of": "bf"}) == ("bf", "")
    for rid in ("bn", "bo", "br", "nope", 7):
        assert app.check_resume(BASE | {"resume_of": rid})[0] is None
    seen, events = [], []
    monkeypatch.setattr(orchestrator, "build", lambda ctx, emit: seen.append(ctx) or {"type": "failed"})
    for i, rid in enumerate(("bf", "bn")):
        lu = db.save_lookup("s1", "q", "none", [])
        app.chef_build(BASE | {"lookup_id": lu, "build_id": f"b{i}", "resume_of": rid}, lambda: events.append)
    assert [c.resume_of for c in seen] == ["bf", None]
    assert len(events) == 1 and "ignored: no plan checkpoint" in events[0]["msg"]   # a trace, never a 400
