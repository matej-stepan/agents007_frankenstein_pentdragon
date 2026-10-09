"""Big Chef offline: a scripted LLM (keyed by role and tool, so the parallel fan-out is deterministic) and a fake
runner walk P1-P5 on a temp DB, then repair and improve builds make v2.

No network, no container, no tool code is executed on the host (the runner is faked).
"""

import ast
import json
import threading
import time

import pytest
from shed import llm, runner
from shed.chef import orchestrator as chef
from shed.db import DB

COST = 0.01
OK = {"by": "operator", "mode": "once"}
SAMPLES = {"https://example.com/api/search": '{"items": [{"id": 7, "url": "https://example.com/i/7", "price": 1}]}'}


def resp(content="", calls=(), finish=None):
    msg = {"role": "assistant", "content": content, "reasoning_content": "thinking..."}
    if calls:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg, "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": {}, "x_meter": {"cost_usd": COST}}


def spec(name, grade, uses=(), network=True):
    return {"name": name, "grade": grade, "summary": f"{name}: one line", "description": f"{name} in detail",
            "keywords": [name], "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
            "uses": list(uses), "deps": [], "permissions": {"network": network, "llm_usd": 0, "files": "none"},
            "limits": {"timeout_s": 30}, "examples": [{"args": {"url": "https://example.com"}}]}


def submit(plan, samples=SAMPLES):
    return resp(calls=[("submit_plan", {"plan": plan, "samples": samples})])


FETCH = spec("fetch_page", "small")
ENTRY = spec("listing_search", "big", uses=["fetch_page"], network=False)
TESTS = "```python\nfrom tool import run\n\n\ndef test_a():\n    assert run({'url': 'x'}, None)\n```"
FETCH_CODE = '''import urllib.request


def http_get(url, *, params=None, headers=None, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.geturl(), r.read().decode()


def run(args, shed):
    status, url, text = http_get(args["url"])
    return {"url": url, "status": status, "text": text}  # BUG
'''
FIX = ('<<<<<<< SEARCH\n    return {"url": url, "status": status, "text": text}  # BUG\n=======\n'
       '    return {"url": url, "status": status, "text": text}\n>>>>>>> REPLACE')
ENTRY_CODE = '''def run(args, shed):
    if not args.get("query"):
        raise ValueError("query is required")
    page = shed.call("fetch_page", {"url": "https://example.com/?q=" + args["query"]})
    return {"results": [{"url": page["url"], "reason": "match"}], "warnings": [], "sources": [page["url"]]}
'''
REPAIR = ('<<<<<<< SEARCH\n    page = shed.call("fetch_page", {"url": "https://example.com/?q=" + args["query"]})\n=======\n'
          '    page = shed.call("fetch_page", {"url": "https://example.com/?q=" + str(args["query"])})\n'
          '>>>>>>> REPLACE')
APPROVE = '{"verdict": "approve", "reasons": []}'


def code_reply(code, name="x", note="Limits: one site."):
    return resp(f"```python\n{code}```\n```markdown\n# {name}\n{note}\n```")


def walk_script(over=None):
    """P1 explores, submits a bad plan, then a good one; fetch_page goes red once (BUG), then green."""
    bad = {"entry": ENTRY, "small": []}                   # uses fetch_page, which nobody builds or reuses
    good = {"entry": ENTRY, "small": [FETCH], "notes": "generic fetcher + big entry"}
    s = {"plan": [resp(calls=[("explore", {"query": "fetch web page"})]), submit(bad), submit(good)],
         ("tests", "fetch_page"): [resp(TESTS)], ("tests", "listing_search"): [resp(TESTS)],
         ("code", "fetch_page"): [resp(calls=[("probe", {"code": "print(1)"})]), code_reply(FETCH_CODE, "fetch_page"),
                                  resp(FIX)],
         ("code", "listing_search"): [code_reply(ENTRY_CODE, "listing_search")],
         "security": [resp(APPROVE), resp(APPROVE)]}
    return {**s, **(over or {})}


class FakeLLM:
    def __init__(self, script: dict):
        self.script, self.bodies, self.lock = {k: list(v) for k, v in script.items()}, [], threading.Lock()

    def __call__(self, grant, body, role, tool=None):
        with self.lock:
            self.bodies.append((role, tool, json.loads(json.dumps(body))))  # a copy: the Chef appends later
            return self.script[(role, tool) if (role, tool) in self.script else role].pop(0)

    def models(self, role, tool=None):
        return [b["model"] for r, t, b in self.bodies if r == role and t == tool]


def fake_run_tests(files, timeout_s=120, live=False):
    code = files["tool.py"]
    passed, failed = (0, 3) if code == chef.STUB else (2, 1) if "BUG" in code else (3, 0)
    return runner.TestResult(passed, failed, 0, f"{passed} passed, {failed} failed in 0.01s", 5, False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(chef, "ROLE", dict(chef._ROLE_DEFAULT))   # the defaults, whatever the shell env says
    monkeypatch.setattr(runner, "run_tests", fake_run_tests)
    monkeypatch.setattr(runner, "probe", lambda code, files=None, timeout_s=30: runner.ExecResult(0, "1\n", "", False, 3))
    db = DB(str(tmp_path / "shed.db"))
    lookup_id = db.save_lookup("s1", "houses in Brno", "none", [])
    n = iter(range(100))

    def run(script, **kw):
        fake, events = FakeLLM(script), []
        monkeypatch.setattr(llm, "chat", fake)
        ctx = chef.BuildCtx(db=db, grant="g", task="cheapest house in Brno-venkov", need="search listings",
                            lookup_id=lookup_id, session_id="s1", build_id=f"b{next(n)}", **kw)
        return chef.build(ctx, events.append), events, fake

    def ctx():
        return chef.BuildCtx(db=db, grant="g", task="cheapest house in Brno-venkov", need="n", lookup_id=None,
                             session_id="s1", build_id="bc")

    return db, run, ctx


def test_build_walks_p1_to_p5_then_repair(env):
    db, run, _ = env
    h, events, fake = run(walk_script())

    assert h["type"] == "handoff" and events[-1] == h, h
    assert [(t["name"], t["status"], t["tests"], t["verdict"]) for t in h["tree"]] == [
        ("fetch_page", "new", "3/3", "approve"), ("listing_search", "new", "3/3", "approve")]
    assert [t["iterations"] for t in h["tree"]] == [2, 1]
    assert h["entry"] == "listing_search" and "one site" in h["skill"]
    assert h["cost_usd"] == pytest.approx(COST * 11) == pytest.approx(sum(t["cost_usd"] for t in h["tree"]))
    assert {e["phase"] for e in events if e["type"] == "trace"} == {"P1", "P2", "P3", "P4", "P5"}
    assert [e["kind"] for e in events if e["type"] == "test_run"].count("stub_sanity") == 2
    for t in h["tree"]:                                    # rule 3 data is in the DB, only approval is missing
        assert db.check_register(t["content_hash"], OK)[1] == []
    for _, _, body in fake.bodies:                         # thinking mode: reasoning kept, no forced tool_choice
        assert "tool_choice" not in body
        assert all("reasoning_content" in m for m in body["messages"] if m.get("tool_calls"))
    tests_req = next(b for r, t, b in fake.bodies if r == "tests")["messages"][1]["content"]
    assert "Real data samples" in tests_req and "example.com/i/7" in tests_req      # P2 gets the real sample

    for t in h["tree"]:
        db.register(t["content_hash"], OK)
    db.record_event("invoked", "listing_search", 1, {"invoke_id": "inv1", "ok": False, "error": "TypeError: query"})
    h2, _, _ = run({"plan": [submit({"entry": {"extend": "listing_search@v1"}, "small": [{"reuse": "fetch_page@v1"}],
                                     "notes": "query is not always a str"})],
                    "tests": [resp(TESTS)], "code": [resp(REPAIR)], "security": [resp(APPROVE)]},
                   repair_of={"tool": "listing_search", "invoke_id": "inv1"})

    assert h2["type"] == "handoff", h2
    assert [(t["name"], t["status"]) for t in h2["tree"]] == [("fetch_page", "reused"), ("listing_search", "new")]
    assert db.get_draft(h2["tree"][-1]["content_hash"])["manifest"]["parent"] == "listing_search@v1"
    assert db.register(h2["tree"][-1]["content_hash"], OK)["version"] == 2


def test_cut_code_reply_is_not_an_iteration(env):                                    # (a)
    _, run, _ = env
    good = FETCH_CODE.replace("  # BUG", "")
    h, _, fake = run(walk_script({("code", "fetch_page"): [resp("Let me analyse the page first...", finish="length"),
                                                           code_reply(good, "fetch_page")]}))
    assert h["type"] == "handoff", h
    assert h["tree"][0]["name"] == "fetch_page" and h["tree"][0]["iterations"] == 1
    last = [b for r, t, b in fake.bodies if t == "fetch_page" and r == "code"][-1]["messages"]
    assert last[-1] == {"role": "user", "content": chef.CUT} and last[-2]["reasoning_content"]


def test_role_models_and_escalation(env):                                           # (b)
    _, run, _ = env
    h, _, fake = run(walk_script())
    assert h["type"] == "handoff", h
    plan = [b for r, _, b in fake.bodies if r == "plan"]
    assert {(b["model"], b["reasoning_effort"], b["max_tokens"]) for b in plan} == {("deepseek-v4-pro", "high", 32768)}
    # probe turn + first code: flash; the reply after the red test run (BUG): v4-pro
    assert fake.models("code", "fetch_page") == ["deepseek-flash", "deepseek-flash", "deepseek-v4-pro"]
    assert fake.models("code", "listing_search") == ["deepseek-flash"]
    assert {b["model"] for r, _, b in fake.bodies if r in ("tests", "security")} == {"deepseek-flash"}
    assert all(b["max_tokens"] == 32768 for r, _, b in fake.bodies if r == "code")


def test_fan_out_builds_tools_in_parallel(env, monkeypatch):                        # (c)
    _, run, _ = env

    def slow(files, timeout_s=120, live=False):
        time.sleep(1)
        return fake_run_tests(files, timeout_s, live)

    monkeypatch.setattr(runner, "run_tests", slow)
    names = ["alpha_tool", "beta_tool", "gamma_tool"]
    small = [spec(n, "small", network=False) for n in names]
    entry = spec("combo_search", "big", uses=names, network=False)
    script = {"plan": [submit({"entry": entry, "small": small}, None)], "security": [resp(APPROVE)] * 4}
    for n in names + ["combo_search"]:
        script[("tests", n)] = [resp(TESTS)]
        script[("code", n)] = [code_reply("def run(args, shed):\n    return {}\n", n)]
    t0 = time.monotonic()
    h, _, _ = run(script)
    wall = time.monotonic() - t0
    assert h["type"] == "handoff", h
    assert [t["name"] for t in h["tree"]] == names + ["combo_search"]
    assert wall < 3.0, f"{wall:.2f}s: 4 tools x 2 test runs of 1 s each must overlap"


def test_plan_trivia_is_fixed_and_semantics_rejected(env):                          # (d) + P0-5 + P1-2
    _, _, ctx = env
    c = chef.Chef(ctx(), lambda e: None)
    entry = {k: v for k, v in ENTRY.items() if k != "grade"} | {"summary": "s" * 150, "keywords": list("abcdefghijklmnopqrs")}
    del entry["permissions"], entry["limits"]
    errs, plan = c.check_plan({"entry": entry, "small": [FETCH]}, SAMPLES)
    assert errs == [], errs
    e = plan["entry"]
    assert e["grade"] == "big" and len(e["summary"]) <= 120 and len(e["keywords"]) == 16
    assert e["permissions"] == chef.DEFAULT_PERMS and e["limits"]["timeout_s"] == 60

    errs, _ = c.check_plan({"entry": entry, "small": [FETCH]})                         # networked, no sample
    assert any("samples" in x for x in errs)
    leaky = {**entry, "input_schema": {"type": "object", "properties": {"district": {"type": "string",
                                                                                     "default": "brno-venkov"}}}}
    errs, _ = c.check_plan({"entry": leaky, "small": [FETCH]}, SAMPLES)               # a task value as a default
    assert any("district" in x and "task" in x for x in errs)
    errs, _ = c.check_plan({"entry": spec("page_grab", "small"), "small": []}, SAMPLES)  # empty registry
    assert any("registry is empty" in x for x in errs)


def test_improve_ok_invocation_keeps_old_tests(env):                                 # (e)
    db, run, _ = env
    h, _, _ = run(walk_script())
    for t in h["tree"]:
        db.register(t["content_hash"], OK)
    v1_tests = db.get_version("listing_search")["files"]["test_tool.py"]
    db.record_event("invoked", "listing_search", 1, {"invoke_id": "inv2", "ok": True})
    extra = ("```python\nfrom tool import run\nfrom shed_sdk.testing import MockShed\n\n\ndef test_a():\n"
             "    assert run({'query': 'house'}, MockShed())\n\n\ndef test_url_in_every_row():\n    assert True\n```")
    plan = {"entry": {"extend": "listing_search@v1", "description": "listing_search; copies the item url"},
            "small": [{"reuse": "fetch_page@v1"}], "notes": "the url field is never copied"}
    h2, _, fake = run({"plan": [submit(plan)], "tests": [resp(extra)], "code": [resp(REPAIR)],
                       "security": [resp(APPROVE)]},
                      repair_of={"tool": "listing_search", "invoke_id": "inv2", "problem": "url empty in every row",
                                 "args": {"query": "house"}, "error": None})

    assert h2["type"] == "handoff", h2
    d = db.get_draft(h2["tree"][-1]["content_hash"])
    assert d["manifest"]["parent"] == "listing_search@v1" and h2["tree"][-1]["parent"] == "listing_search@v1"
    assert d["manifest"]["examples"][0]["args"] == {"query": "house"}
    names = {n.name for n in ast.parse(d["files"]["test_tool.py"]).body if isinstance(n, ast.FunctionDef)}
    old = {n.name for n in ast.parse(v1_tests).body if isinstance(n, ast.FunctionDef)}
    assert old and old <= names and {"test_a_v2", "test_url_in_every_row"} <= names
    first = next(b for r, _, b in fake.bodies if r == "plan")["messages"][1]["content"]
    assert first.startswith("Improve listing_search@v1") and "url empty in every row" in first and "def run" in first
    code_req = next(b for r, _, b in fake.bodies if r == "code")["messages"][1]["content"]
    assert "url empty in every row" in code_req and "never copied" in code_req



def test_improve_can_target_a_sub_tool(env):
    db, run, _ = env
    h, _, _ = run(walk_script())
    for t in h["tree"]:
        db.register(t["content_hash"], OK)
    db.record_event("invoked", "listing_search", 1, {"invoke_id": "inv3", "ok": True})
    plan = {"entry": {"reuse": "listing_search@v1"},
            "small": [{"extend": "fetch_page@v1", "description": "fetch_page; follows redirects"}], "notes": "no redirects"}
    h2, _, _ = run({"plan": [submit(plan)], "tests": [resp(TESTS)], "code": [resp(FIX.replace("  # BUG", "", 1))],
                    "security": [resp(APPROVE)]},
                   repair_of={"tool": "listing_search", "invoke_id": "inv3", "problem": "0 results", "args": {}})
    assert h2["type"] == "handoff", h2
    assert [(t["name"], t["status"]) for t in h2["tree"]] == [("fetch_page", "new"), ("listing_search", "reused")]
    assert h2["tree"][0]["parent"] == "fetch_page@v1" and h2["entry"] == "listing_search" and "one site" in h2["skill"]
    assert db.register(h2["tree"][0]["content_hash"], OK)["version"] == 2


def test_runner_deselects_live_tests_unless_asked(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(runner, "_rundir", lambda files: tmp_path / "run")
    monkeypatch.setattr(runner, "_exec", lambda cmd, d, **kw: seen.append(cmd) or runner.ExecResult(
        0, "2 passed, 1 deselected in 0.1s", "", False, 1))
    assert runner.run_tests({"tool.py": ""}).passed == 2
    runner.run_tests({"tool.py": ""}, 60, live=True)
    assert "not live" in seen[0] and "not live" not in seen[1]

def test_meter_refusal_ends_with_failed(env, monkeypatch):
    _, _, ctx = env

    def refuse(*a, **k):
        raise llm.CapExceeded({"cap": "build", "limit": 0.6})

    monkeypatch.setattr(llm, "chat", refuse)
    events = []
    out = chef.build(ctx(), events.append)
    assert out["type"] == "failed" and "cap" in out["reason"] and events[-1] == out


def test_late_plan_rejection_gets_a_fix_turn(env, monkeypatch):
    _, _, ctx = env
    fetch = {k: v for k, v in FETCH.items() if k != "grade"}   # a new item in "small" defaults to grade small
    probe = resp(calls=[("probe", {"code": "print(1)"})])
    fake = FakeLLM({"plan": [probe] * (chef.PLAN_TURNS - 1) + [
        submit({"entry": ENTRY, "small": []}),                   # rejected on the last turn
        submit({"entry": ENTRY, "small": [fetch]})]})
    monkeypatch.setattr(llm, "chat", fake)
    plan = chef.Chef(ctx(), lambda e: None).plan()
    assert plan["entry"]["name"] == "listing_search" and plan["new"][0]["grade"] == "small"
