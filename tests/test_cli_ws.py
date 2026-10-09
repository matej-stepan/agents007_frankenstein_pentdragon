"""Workspace jail for ws_read / ws_write."""

import os

import pytest
from taltempla.ws_tools import MAX_READ, WsError, ws_read, ws_write


def test_write_read_roundtrip(tmp_path):
    r = ws_write(tmp_path, "out/a/b.txt", "hello")
    assert r == {"ok": True, "path": "workspace/out/a/b.txt"}
    assert ws_read(tmp_path, "out/a/b.txt") == "hello"
    assert ws_read(tmp_path, "workspace/out/a/b.txt") == "hello"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", "~/x", ""])
def test_escapes_refused(tmp_path, bad):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(WsError):
        ws_write(ws, bad, "x")
    with pytest.raises(WsError):
        ws_read(ws, bad)


def test_symlink_escape_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret").write_text("key")
    os.symlink(tmp_path / "secret", ws / "link")
    with pytest.raises(WsError):
        ws_read(ws, "link")


def test_read_truncates_at_64k(tmp_path):
    (tmp_path / "big.txt").write_text("x" * (MAX_READ + 10))
    out = ws_read(tmp_path, "big.txt")
    assert out.startswith("x" * MAX_READ) and "truncated" in out
