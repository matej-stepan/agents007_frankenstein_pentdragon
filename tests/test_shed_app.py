"""shedd /chef/build gap rule for repair and improve (temp DB, no build runs)."""

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
