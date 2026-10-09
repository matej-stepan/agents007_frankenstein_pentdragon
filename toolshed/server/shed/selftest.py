"""python3 -m shed.selftest: sandbox and registry checks, run as root inside the toolshed container.

Uses a temp DB, a temp runtime socket and tiny inline fixture tools (test-only; never the real /data/shed.db).
Prints PASS/FAIL lines and exits non-zero on a failure.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import textwrap

from shed import runner
from shed.chain import MAX_SUBCALLS, Chain
from shed.db import DB, RegisterRefused
from shed.lookup import explore, lookup

FAILS: list[str] = []
APPROVAL = {"mode": "once", "by": "operator"}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        FAILS.append(name)


def fixture(name: str, body: str, uses=(), grade="small", llm_usd=0.0, timeout_s=30) -> tuple[dict, dict]:
    m = {"name": name, "grade": grade, "summary": f"selftest fixture {name.replace('_', ' ')}",
         "description": "Test-only fixture tool for the shedd selftest.", "keywords": ["selftest", name],
         "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "uses": list(uses), "deps": [],
         "permissions": {"network": False, "llm_usd": llm_usd, "files": "none"},
         "limits": {"timeout_s": timeout_s}, "examples": [{"args": {}}]}
    mocks = "".join(f"    s.mock({u!r}, {{'ok': 1}})\n" for u in uses)
    test = ("from tool import run\nfrom shed_sdk.testing import MockShed\n\n\ndef test_returns_dict():\n"
            f"    s = MockShed()\n    s.mock_llm('x')\n{mocks}    assert isinstance(run({{}}, s), dict)\n")
    return m, {"tool.py": textwrap.dedent(body), "test_tool.py": test, "SKILL.md": f"# {name}\nSelftest fixture."}


def install(db: DB, m: dict, files: dict) -> dict:
    h = db.save_draft("selftest", m, files)["content_hash"]
    tr = runner.run_tests(files)
    db.record_test_run(h, "tests", tr.passed, tr.failed + tr.errors, tr.log)
    if tr.failed or tr.errors or not tr.passed:
        raise RuntimeError(f"fixture {m['name']} tests failed:\n{tr.log[-1500:]}")
    db.record_review(h, "approve", "selftest fixture")
    return db.register(h, APPROVAL)


PROBE = """
import json, os, socket
r = {"uid": os.getuid(), "env": sorted(os.environ)}
for p in ("/data/shed.db", "/proc/1/environ"):
    try:
        open(p, "rb").read(1); r[p] = "READ"
    except Exception as e:
        r[p] = type(e).__name__
for p in ("/data", "/run/meter"):
    try:
        os.listdir(p); r[p] = "LISTED"
    except Exception as e:
        r[p] = type(e).__name__
try:
    s = socket.socket(socket.AF_UNIX); s.connect("/run/meter/meter.sock"); r["meter.sock"] = "CONNECTED"
except Exception as e:
    r["meter.sock"] = type(e).__name__
try:
    open("/opt/shed/server/shed/pwned.py", "w"); r["/opt/shed"] = "WROTE"
except Exception as e:
    r["/opt/shed"] = type(e).__name__
print(json.dumps(r))
"""

CALLER = """
from shed_sdk import ShedError
def run(args, shed):
    out = {"inside": shed.call("st_double", {"x": 3})}
    try:
        shed.call("st_secret", {})
        out["outside"] = "ALLOWED"
    except ShedError as e:
        out["outside"] = str(e)
    return out
"""

LINK = """
from shed_sdk import ShedError
def run(args, shed):
    try:
        return {"me": "%s", "next": shed.call("%s", {})}
    except ShedError as e:
        return {"me": "%s", "refused": str(e)}
"""

FAN = """
from shed_sdk import ShedError
def run(args, shed):
    n = 0
    try:
        for _ in range(%d):
            shed.call("st_double", {"x": 1}); n += 1
    except ShedError as e:
        return {"ok_calls": n, "error": str(e)}
    return {"ok_calls": n, "error": None}
"""

NOLLM = """
from shed_sdk import ShedError
def run(args, shed):
    try:
        return {"llm": shed.llm("hi")}
    except ShedError as e:
        return {"refused": str(e)}
"""

LLM = """
def run(args, shed):
    return {"said": shed.llm("Reply with the single word: ok", max_tokens=64)}
"""

LLMCOMBO = """
def run(args, shed):
    return {"double": shed.call("st_double", {"x": 21}), "llm": shed.call("st_llm", {})}
"""


def metered(db: DB, chain: Chain, grant: str) -> None:
    """M1: a chained root run whose sub-tool calls shed.llm through the host meter (SELFTEST_GRANT)."""
    install(db, *fixture("st_llm", LLM, llm_usd=0.01))
    install(db, *fixture("st_llmcombo", LLMCOMBO, uses=["st_double", "st_llm"], grade="big"))
    r = chain.invoke("st_llmcombo", {}, grant=grant, run_id="selftest", session_id="selftest")
    res = r.get("result") or {}
    check("metered chain: root run ok", r.get("ok") is True, json.dumps(r)[:600])
    check("metered chain: both subcalls ran", sorted(s["name"] for s in r.get("subcalls", [])) == ["st_double", "st_llm"])
    check("metered chain: shed.llm answered via the meter", isinstance((res.get("llm") or {}).get("said"), str))
    check("metered chain: LLM cost is charged to the root run", r.get("llm_cost_usd", 0) > 0, str(r.get("llm_cost_usd")))
    print(f"INFO metered chain: {json.dumps({k: r.get(k) for k in ('subcalls', 'llm_cost_usd', 'result')})[:400]}")


def main() -> int:
    if os.geteuid() != 0:
        print("FAIL selftest must run as root inside the toolshed container")
        return 2
    tmp = tempfile.mkdtemp(prefix="shed-selftest-")
    db = DB(os.path.join(tmp, "selftest.db"))
    chain = Chain(db, sock=f"/run/shed/selftest-{os.getpid()}.sock")
    chain.serve()
    try:
        # 1. The sandbox: the tool user cannot read the DB, PID 1's env (admin token) or the meter socket.
        r = runner.probe(PROBE)
        try:
            p = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            p = {}
        check("probe runs as uid 1000", p.get("uid") == 1000, r.stderr[-500:])
        check("tool cannot read /data/shed.db", p.get("/data/shed.db") not in (None, "READ"), str(p.get("/data/shed.db")))
        check("tool cannot list /data", p.get("/data") not in (None, "LISTED"), str(p.get("/data")))
        check("tool cannot read /proc/1/environ", p.get("/proc/1/environ") not in (None, "READ"))
        check("tool cannot list /run/meter", p.get("/run/meter") not in (None, "LISTED"))
        check("tool cannot connect to the meter socket", p.get("meter.sock") not in (None, "CONNECTED"))
        check("tool cannot write /opt/shed", p.get("/opt/shed") not in (None, "WROTE"))
        allowed_env = {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "SHED_SOCK", "SHED_RUN_TOKEN",
                       "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "LC_CTYPE"}
        check("tool env is clean (env -i)", set(p.get("env", ["?"])) <= allowed_env, str(p.get("env")))
        t = runner.probe("import time; time.sleep(30)", timeout_s=2)
        check("runner timeout kills a slow run", t.timed_out and t.duration_ms < 10000, f"{t}")

        # 2. Register needs a passed test run, an approve review and an operator approval (same content hash).
        m, f = fixture("st_unreg", "def run(args, shed):\n    return {}\n")
        h = db.save_draft("selftest", m, f)["content_hash"]

        def refused(step: str) -> None:
            try:
                db.register(h, APPROVAL if step != "approval" else {"mode": "once", "by": "agent"})
                check(f"register refuses: {step}", False, "it registered")
            except RegisterRefused:
                check(f"register refuses: {step}", True)

        refused("no test run")
        db.record_test_run(h, "tests", 1, 1, "1 failed")
        refused("failed test run only")
        db.record_test_run(h, "tests", 1, 0, "1 passed")
        refused("no review")
        db.record_review(h, "reject", "no")
        refused("reject review")
        db.record_review(h, "approve", "ok")
        refused("approval")
        f2 = {**f, "tool.py": f["tool.py"] + "# changed\n"}
        h2 = db.save_draft("selftest", m, f2)["content_hash"]
        try:
            db.register(h2, APPROVAL)
            check("register refuses: changed code (new content hash)", False)
        except RegisterRefused:
            check("register refuses: changed code (new content hash)", True)
        check("register accepts with all three", db.register(h, APPROVAL)["version"] == 1)

        # 3. Append-only triggers.
        for sql in ("UPDATE versions SET grade = 'big'", "DELETE FROM events", "UPDATE test_runs SET passed = 9"):
            try:
                with db.lock:
                    db.conn.execute(sql)
                check(f"append-only: {sql.split()[0]} {sql.split()[1 if sql[0] == 'U' else 2]}", False)
            except sqlite3.DatabaseError as e:
                check(f"append-only: {sql.split()[0]} {sql.split()[1 if sql[0] == 'U' else 2]}", "append-only" in str(e))

        # 4. Chaining: inside uses is allowed, outside uses is refused.
        install(db, *fixture("st_double", "def run(args, shed):\n    return {'n': args.get('x', 1) * 2}\n"))
        install(db, *fixture("st_secret", "def run(args, shed):\n    return {'secret': 1}\n"))
        install(db, *fixture("st_combo", CALLER, uses=["st_double"], grade="big"))
        r = chain.invoke("st_combo", {}, grant="no-grant")
        res = r.get("result") or {}
        check("chained call inside uses works", res.get("inside") == {"n": 6}, json.dumps(r)[:400])
        check("call outside uses is refused", "not in its uses" in str(res.get("outside")), str(res.get("outside")))
        check("subcalls are reported", [s["name"] for s in r.get("subcalls", [])] == ["st_double"])

        # 5. Depth limit (<= 3) and subcall limit (<= 20 per root).
        chain_names = [f"st_link{i}" for i in range(5)]
        install(db, *fixture(chain_names[-1], "def run(args, shed):\n    return {'me': 'leaf'}\n"))
        for i in range(3, -1, -1):  # the root link is big: the agent runs only big tools (D53)
            a, b = chain_names[i], chain_names[i + 1]
            install(db, *fixture(a, LINK % (a, b, a), uses=[b], grade="big" if i == 0 else "small"))
        r = chain.invoke(chain_names[0], {}, grant="no-grant")
        node, depth = r.get("result") or {}, 0
        while "next" in node:
            node, depth = node["next"], depth + 1
        check("depth limit stops the 4th nested call", depth == 3 and "depth limit" in str(node.get("refused")),
              json.dumps(r.get("result"))[:400])
        install(db, *fixture("st_fan", FAN % (MAX_SUBCALLS + 5), uses=["st_double"], grade="big", timeout_s=120))
        r = chain.invoke("st_fan", {}, grant="no-grant")
        res = r.get("result") or {}
        check(f"subcall limit stops call {MAX_SUBCALLS + 1}", res.get("ok_calls") == MAX_SUBCALLS
              and "subcall limit" in str(res.get("error")), json.dumps(r)[:400])

        # 6. LLM needs permissions.llm_usd > 0; a bad run token is refused.
        install(db, *fixture("st_nollm", NOLLM, uses=["st_double"], grade="big"))
        res = chain.invoke("st_nollm", {}, grant="no-grant").get("result") or {}
        check("llm refused when llm_usd = 0", "no LLM permission" in str(res.get("refused")), str(res))
        from shed_sdk import Shed, ShedError

        try:
            Shed(sock=chain.sock, token="rt_bogus").registry()
            check("bad run token is refused", False)
        except ShedError:
            check("bad run token is refused", True)

        # 7. Lookup over the temp registry: the agent lookup gives big tools only; explore (the Chef) gives all.
        q = "double a number selftest fixture st_double"
        lu = lookup(db, q, "s1")
        check("lookup gives big tools only", bool(lu["rows"]) and all(row["grade"] == "big" for row in lu["rows"]),
              json.dumps(lu)[:300])
        check("explore finds a small fixture", any(row["name"] == "st_double" for row in explore(db, q, k=20)))
        check("lookup on nonsense is fit none", lookup(db, "qwxzv plorf", "s1")["fit"] == "none")

        # 8. Optional (make m1): the metered chain through the host meter, with a tool_run grant from the host.
        if os.environ.get("SELFTEST_GRANT"):
            metered(db, chain, os.environ["SELFTEST_GRANT"])
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("selftest ran to the end", False, f"{type(e).__name__}: {e}")
    finally:
        chain.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'FAIL' if FAILS else 'PASS'} selftest: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
