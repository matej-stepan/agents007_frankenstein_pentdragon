"""Loop rules: the tool_run grant cap covers the whole chain; big_chef needs current gap evidence."""

from taltempla.loop import MAX_BUILDS, Agent, chain_llm_cap, smoke_problems, stale_gap
from taltempla.shed_client import ShedError


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


def test_exit_words_never_reach_the_model():
    from taltempla.main import is_exit_word

    assert all(is_exit_word(w) for w in ("exit", " QUIT ", "exit()", "quit()", ":q", ":wq", "q", "Bye"))
    assert not any(is_exit_word(w) for w in ("exit the loop", "/exit", "quite", ""))
