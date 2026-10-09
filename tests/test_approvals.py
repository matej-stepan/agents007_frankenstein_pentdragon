"""Use approvals: "always" persists by permission hash; "once" is consumed."""

import json

from taltempla.approvals import Approvals


def test_always_is_keyed_by_perm_hash_and_persists(tmp_path):
    p = tmp_path / "permissions.json"
    a = Approvals(p)
    a.allow_always("h" * 64, "fetch_page", 1)
    assert json.loads(p.read_text())["h" * 64]["name"] == "fetch_page"
    b = Approvals(p)
    assert b.take("h" * 64) and b.take("h" * 64)
    assert not b.take("g" * 64)  # a new permission hash (e.g. new deps) needs a new approval


def test_once_is_consumed_and_not_persisted(tmp_path):
    p = tmp_path / "permissions.json"
    a = Approvals(p)
    a.allow_once("abc")
    assert a.take("abc") and not a.take("abc")
    assert not p.exists()


def test_revoke_by_name_or_hash_prefix(tmp_path):
    a = Approvals(tmp_path / "permissions.json")
    a.allow_always("aaaaaaaa11", "x", 1)
    a.allow_always("bbbbbbbb22", "y", 2)
    assert [r["name"] for r in a.revoke("x")] == ["x"]
    assert [r["name"] for r in a.revoke("bbbbbb")] == ["y"]
    assert a.entries() == [] and Approvals(tmp_path / "permissions.json").entries() == []
