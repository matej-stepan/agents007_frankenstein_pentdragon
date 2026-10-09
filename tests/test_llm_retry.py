"""llm.request_json: one retry when the connection drops before any response, never on a timeout."""

import http.client

import pytest
from taltempla import llm


class FakeConn:
    def __init__(self, script):
        self.script = script

    def request(self, *a, **kw):
        pass

    def getresponse(self):
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self):
        pass


class Resp:
    status = 200

    def read(self):
        return b'{"ok": 1}'

    def getheader(self, name):
        return None


def run(monkeypatch, script):
    n = []
    monkeypatch.setattr(llm, "TRANSIENT_BACKOFF", 0)
    monkeypatch.setattr(llm, "_connection", lambda *a: (n.append(1), FakeConn(script))[1:] + ("",))
    try:
        return llm.post_json({}, url="http://x.invalid"), len(n)
    except llm.LLMError as e:
        return e, len(n)


def test_dropped_connection_retries_once(monkeypatch):
    assert run(monkeypatch, [http.client.RemoteDisconnected("closed"), Resp()]) == ({"ok": 1}, 2)
    err, n = run(monkeypatch, [ConnectionResetError(), BrokenPipeError(), Resp()])
    assert n == 2 and err.status == 0 and "BrokenPipeError" in str(err)


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionAbortedError()])
def test_timeout_and_other_errors_never_retry(monkeypatch, exc):
    err, n = run(monkeypatch, [exc, Resp()])
    assert n == 1 and isinstance(err, llm.LLMError) and err.status == 0
