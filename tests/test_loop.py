"""Loop rules: the tool_run grant cap covers the whole chain; big_chef needs current gap evidence;
use_tool runs only big tools."""

import pytest
from taltempla.loop import MAX_BUILDS, Agent, chain_llm_cap, smoke_problems, stale_gap
from taltempla.shed_client import ShedError


@pytest.fixture(autouse=True)
def _auto_gate(monkeypatch):
    monkeypatch.setenv("TALTEMPLA_GATE", "auto")


def test_chain_llm_cap_sums_reachable_tools():
    def t(llm, uses=()):
        return {"permissions": {"llm_usd": llm}, "uses": list(uses)}

    tools = {"big": t(0, ["a", "b"]), "a": t(0.05, ["c"]), "b": t(0), "c": t(0.02, ["a"]), "x": t(0.2)}
    assert chain_llm_cap("big", tools) == 0.07  # a + c once each (cycle-safe); x is not reachable
    assert chain_llm_cap("missing", tools) == 0.0


def test_stale_gap():
    looks = [("l1", "none", []), ("l2", "good", ["estate_search"]), ("l3", "none", [])]
    assert stale_gap("l1", looks) == ["estate_search"]   # a newer lookup found a good fit: no build on l1
    assert stale_gap("l2", looks) == [] and stale_gap("l3", looks) == [] and stale_gap("x", looks) == []


def test_smoke_problems():
    rows = [{"title": "a", "price": None, "url": "u1"}, {"title": "b", "url": "u2"}]
    assert smoke_problems({"results": rows, "warnings": [], "sources": []}) == ["results[].price is null in all 2 rows"]
    assert smoke_problems({"results": [], "warnings": ["blocked"]}) == ["results is empty", 'warnings: ["blocked"]']
    assert smoke_problems(None) == ["the result is empty"] and smoke_problems({"results": rows[:1]}) == []
    assert smoke_problems({"results": [{"p": 1}, {"p": None}]}) == [] and smoke_problems("text") == []


class FakeMeter:
    def grant(self, *a, **k):
        return "g"

    def revoke(self, g):
        pass


class FakeShed:
    def __init__(self, status=None):
        self.status, self.bodies = status, []

    def chef_build(self, body):
        self.bodies.append(body)
        if self.status:
            raise ShedError(self.status, "gap rule: refused")
        yield {"type": "failed", "reason": "test", "cost_usd": 0.0}


def agent(shed, tmp_path):
    return Agent(meter=FakeMeter(), shed=shed, session_grant="s", session_id="s1", root=tmp_path, approvals=None,
                 model="m", caps={"build": 0.6, "run": 2.0})


def test_refused_build_does_not_use_the_budget(tmp_path):
    a = agent(FakeShed(400), tmp_path)
    for _ in range(MAX_BUILDS + 1):  # every request is refused (400): none counts
        assert a.call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})["status"] == 400
    assert a.builds == 0
    a.shed = FakeShed()
    assert a.call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})["failed"] == "test"
    assert a.builds == 1


def test_repair_of_gets_the_recorded_args(tmp_path):
    a, shed = agent(FakeShed(), tmp_path), FakeShed()
    a.shed, a.invocations["inv_1"] = shed, {"q": "x"}
    rep = {"tool": "t", "invoke_id": "inv_1", "problem": "price is null", "args": {"evil": 1}}
    a.call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l", "repair_of": rep})
    assert shed.bodies[0]["repair_of"] == {"tool": "t", "invoke_id": "inv_1", "problem": "price is null",
                                           "args": {"q": "x"}}


def test_use_tool_refuses_a_small_tool_before_the_gate(tmp_path):
    shed = FakeShed()
    shed.tools = lambda: [{"name": "download_file", "grade": "small", "perm_hash": "p"}]
    r = agent(shed, tmp_path).call_tool("use_tool", {"name": "download_file", "args": {"url": "u"}})
    assert r["status"] == 400 and "small building block" in r["error"]  # approvals=None: the gate never ran


def test_exit_words_never_reach_the_model():
    from taltempla.main import is_exit_word

    assert all(is_exit_word(w) for w in ("exit", " QUIT ", "exit()", "quit()", ":q", ":wq", "q", "Bye"))
    assert not any(is_exit_word(w) for w in ("exit the loop", "/exit", "quite", ""))


# ---- G2 build options and the G1 Waiter ----------------------------------------------------------------------
def test_build_options_clamp():
    from taltempla.gate import clamp_options

    o = clamp_options({"cap_usd": 5, "effort": "ultra", "cap_seconds": "abc", "plan_seconds": 9999}, 0.4)
    assert o == {"cap_usd": 0.4, "effort": None, "cap_seconds": 360, "plan_seconds": 150}  # None = role efforts
    assert clamp_options({"cap_seconds": 120, "plan_seconds": 150}, 1)["plan_seconds"] == 50  # P1 keeps its share
    assert clamp_options({"cap_usd": -1, "effort": "max", "cap_seconds": 10, "plan_seconds": 1}, 9) == {
        "cap_usd": 1.0, "effort": "max", "cap_seconds": 60, "plan_seconds": 30}


def test_auto_mode_skips_the_waiter(tmp_path, monkeypatch):
    from taltempla import waiter

    monkeypatch.setenv("TALTEMPLA_GATE", "auto")
    monkeypatch.setattr(waiter, "diagnose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("paid call")))
    shed = FakeShed()
    r = agent(shed, tmp_path).call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})
    assert r == {"failed": "test", "cost_usd": 0.0}
    assert shed.bodies[0]["options"] == {"cap_seconds": 360, "plan_seconds": 150}  # no effort: the role defaults


def test_retry_sends_advice_and_asks_again(tmp_path, monkeypatch):
    from taltempla import gate, waiter

    monkeypatch.setenv("TALTEMPLA_GATE", "ask")
    picks = iter(["start", "retry", "start", "continue"])  # build gate, retry gate, build gate, retry gate
    monkeypatch.setattr(gate, "ask", lambda *a, **k: next(picks))
    monkeypatch.setattr(waiter, "diagnose", lambda *a, **k: {"cause": "403 on the source", "advice": "use the API",
                                                             "ok": True})
    shed = FakeShed()
    a = agent(shed, tmp_path)
    r = a.call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})
    assert r["failed"] == "test" and r["diagnosis"] == "403 on the source"
    assert "advice" not in shed.bodies[0] and shed.bodies[1]["advice"] == "use the API"
    assert shed.bodies[0]["build_id"] != shed.bodies[1]["build_id"] and a.builds == 1  # a retry is one build


def test_refused_retry_keeps_the_first_failure(tmp_path, monkeypatch):
    from taltempla import gate, waiter

    monkeypatch.setenv("TALTEMPLA_GATE", "ask")
    picks = iter(["start", "retry", "start"])
    monkeypatch.setattr(gate, "ask", lambda *a, **k: next(picks))
    monkeypatch.setattr(waiter, "diagnose", lambda *a, **k: {"cause": "c", "advice": "a", "ok": True})
    shed = FakeShed()
    real = shed.chef_build

    def build(body):  # the toolshed restarted between the attempts
        if shed.bodies:
            raise ShedError(0, "toolshed unreachable")
        return real(body)

    shed.chef_build = build
    r = agent(shed, tmp_path).call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})
    assert r["diagnosis"] == "c" and r["failed"].startswith("test (retry refused: toolshed unreachable")


# ---- cap top-up (cap_hit), checkpoint resume, the chef spinner -------------------------------------------------
class TopupMeter(FakeMeter):
    def __init__(self, left=0.9):
        self.left, self.topups = left, []

    def grant_info(self, g):
        return {"left_usd": self.left}

    def topup(self, g, usd=0.0, seconds=0, deny=False):
        self.topups.append((usd, seconds, deny))


class EventShed(FakeShed):
    def __init__(self, *runs):
        super().__init__()
        self.runs = list(runs)

    def chef_build(self, body):
        self.bodies.append(body)
        yield from self.runs.pop(0)


CAP = {"type": "cap_hit", "kind": "usd", "spent_usd": 0.5, "cap_usd": 0.5, "elapsed_s": 90, "cap_s": 360, "n": 1}
FAIL = {"type": "failed", "reason": "test", "cost_usd": 0.0}


def test_cap_hit_asks_once_per_event_and_clamps_to_the_run_reserve(tmp_path, monkeypatch):
    from taltempla import gate, loop, waiter

    monkeypatch.setenv("TALTEMPLA_GATE", "ask")
    asked = []
    picks = iter(["start", "0.50", "600", "continue"])  # build gate, usd top-up, time top-up, retry gate

    def ask(msg, opts, auto):
        asked.append((msg, opts))
        return next(picks)

    monkeypatch.setattr(gate, "ask", ask)
    monkeypatch.setattr(waiter, "diagnose", lambda *a, **k: {"cause": "c", "advice": "", "ok": True})
    a = agent(EventShed([CAP, CAP, {**CAP, "kind": "time", "n": 2}, FAIL]), tmp_path)
    a.meter = TopupMeter(left=loop.RUN_RESERVE_USD + 0.5)  # room = 0.5: the +$1.00 option is clamped to it
    a.call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})
    assert a.meter.topups == [(0.5, 0, False), (0.0, 600, False)]  # the repeated event (same n) never asked again
    assert asked[1][0] == "Build cap reached ($0.50 of $0.50). Raise it?"
    assert [v for v, _ in asked[1][1]] == ["0.50", "stop"] and [v for v, _ in asked[2][1]] == ["300", "600", "stop"]


def test_cap_hit_auto_mode_stops_the_build(tmp_path):
    a = agent(EventShed([CAP, FAIL]), tmp_path)
    a.meter = TopupMeter()
    assert a.call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})["failed"] == "test"
    assert a.meter.topups == [(0.0, 0, True)]


def test_resume_from_the_checkpoint(tmp_path, monkeypatch):
    from taltempla import gate, waiter

    monkeypatch.setenv("TALTEMPLA_GATE", "ask")
    asked, picks = [], iter(["start", "resume", "start", "continue"])
    monkeypatch.setattr(gate, "ask", lambda msg, opts, auto: asked.append(opts) or next(picks))
    replan = iter([False, True])
    monkeypatch.setattr(waiter, "diagnose", lambda *a, **k: {"cause": "c", "advice": "x", "ok": True,
                                                             "replan": next(replan)})
    plan = {"type": "checkpoint", "stage": "plan", "plan": {"entry": {"name": "e"}}}
    tool = {"type": "checkpoint", "stage": "tool", "tool": "fetch_x", "content_hash": "h"}
    shed = EventShed([plan, tool, tool, FAIL], [plan, FAIL])
    agent(shed, tmp_path).call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})
    assert asked[1][0] == ("resume", "Resume from the checkpoint (plan + 1 green tools)")
    assert "resume_of" not in shed.bodies[0] and shed.bodies[1]["resume_of"] == shed.bodies[0]["build_id"]
    assert shed.bodies[1]["advice"] == "x" and [v for v, _ in asked[3]] == ["retry", "continue"]  # replan: no resume


def test_no_resume_when_the_server_did_not_fail_the_build(tmp_path, monkeypatch):
    from taltempla import gate, waiter

    monkeypatch.setenv("TALTEMPLA_GATE", "ask")
    asked, picks = [], iter(["start", "continue"])
    monkeypatch.setattr(gate, "ask", lambda msg, opts, auto: asked.append(opts) or next(picks))
    monkeypatch.setattr(waiter, "diagnose", lambda *a, **k: {"cause": "c", "advice": "", "ok": True, "replan": False})
    plan = {"type": "checkpoint", "stage": "plan", "plan": {}}
    agent(EventShed([plan]), tmp_path).call_tool("big_chef", {"task": "t", "need": "n", "lookup_id": "l"})
    assert [v for v, _ in asked[1]] == ["retry", "continue"]  # the stream ended, the server build may still run


def test_chef_status_text(monkeypatch):
    from taltempla import ui

    s = ui.ChefStatus(360)
    s.on({"type": "trace", "phase": "P2", "tool": "fetch_x", "cost_usd": 0.1234})
    s.sync(120, 600)
    assert str(s.__rich__()).startswith("chef P2 fetch_x · 0 s since the last step · $0.1234 · 2.0/10 min")
    monkeypatch.setattr(ui, "plain", True)
    with s:
        assert s._st is None  # plain mode (--once): no live display
