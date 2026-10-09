"""Meter: caps, grants, cost math, the unix-socket server. No network (the upstream is a fake)."""

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from taltempla import llm
from taltempla.meter import Meter, MeterAuthError, MeterRefused, call_cost, is_peak

PRICES = Path(__file__).resolve().parents[1] / "cli" / "taltempla" / "prices.json"
USAGE = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200,
         "completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 40}}


def fake_upstream(calls: list):
    def post(body, *, url, headers, **kw):
        calls.append(body)
        return {"model": body["model"], "usage": USAGE, "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "hi", "reasoning_content": "think"}}]}
    return post


@pytest.fixture
def meter(tmp_path):
    m = Meter(ledger_path=tmp_path / "ledger.db", prices_path=PRICES, key="sk-test", base_url="http://up.invalid",
              caps={"run": 2.0, "session": 5.0, "total": 20.0, "build": 0.6}, session_id="s1")
    m.calls = []
    m.upstream = fake_upstream(m.calls)
    yield m
    m.close()


def ledger(m: Meter) -> list[tuple]:
    return m._db.execute("SELECT status, role, hit, miss, out, reasoning, cost_usd FROM calls ORDER BY id").fetchall()


BODY = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 99999}


def test_cost_math_peak_offpeak():
    import json
    prices = json.loads(PRICES.read_text())
    peak, off = datetime(2026, 10, 5, 2, tzinfo=UTC), datetime(2026, 10, 10, 2, tzinfo=UTC)  # Monday / Saturday
    assert is_peak(peak) and not is_peak(off) and not is_peak(datetime(2026, 10, 5, 12, tzinfo=UTC))
    u = {"hit": 1_000_000, "miss": 1_000_000, "out": 1_000_000}
    assert call_cost(prices, "deepseek-flash", u, peak) == pytest.approx(0.006 + 0.30 + 1.20)
    assert call_cost(prices, "deepseek-flash", u, off) == pytest.approx(0.003 + 0.15 + 0.60)
    assert call_cost(prices, "no-such-model", u, off) == pytest.approx(0.753)  # unknown -> flash
    assert llm.usage_tokens({"usage": USAGE}) == {"hit": 800, "miss": 200, "out": 100, "reasoning": 40}


def test_chat_settles_clamps_and_logs(meter):
    run = meter.grant("run", parent=meter.session_grant, cap_usd=None)
    resp = meter.chat(run, BODY, role="main")
    assert meter.calls[0]["max_tokens"] == 16384 and meter.calls[0]["model"] == "deepseek-flash"
    assert resp["x_meter"]["max_tokens"] == 16384
    status, role, hit, miss, out, reasoning, cost = ledger(meter)[0]
    assert (status, role, hit, miss, out, reasoning) == ("ok", "main", 800, 200, 100, 40)
    assert cost == pytest.approx(resp["x_meter"]["cost_usd"], abs=1e-6) and cost > 0
    assert meter.spent(run) == pytest.approx(cost) and meter.status()["session_usd"] == pytest.approx(cost)
    assert meter.status()["cache_pct"] == 80.0
    assert llm.assistant_message(resp)["reasoning_content"] == "think"
    meter.record_saving(None, "fetch_page", 1, 0.05)
    rep = meter.cost_report("s1")
    assert rep["total"]["calls"] == 1 and rep["saved"]["total_usd"] == 0.05 and rep["by_role"][0]["role"] == "main"


def test_over_cap_refused_without_network(meter):
    run = meter.grant("run", parent=meter.session_grant, cap_usd=None)
    tiny = meter.grant("tool_run", parent=run, cap_usd=0.0001, tool="t")
    with pytest.raises(MeterRefused) as e:
        meter.chat(tiny, BODY, role="tool")
    assert e.value.status == 402 and e.value.info["cap"] == "tool_run" and meter.calls == []
    assert ledger(meter)[0][0] == "refused"
    meter.caps["total"] = 0.0  # the total cap over all sessions also refuses
    with pytest.raises(MeterRefused) as e:
        meter.chat(run, BODY)
    assert e.value.info["cap"] == "total"


def test_bad_or_revoked_grant_is_401(meter):
    with pytest.raises(MeterAuthError):
        meter.chat("g-nope", BODY)
    run = meter.grant("run", parent=None, cap_usd=None)
    build = meter.grant("build", parent=run, cap_usd=None)
    meter.revoke(run)  # kills the child too
    with pytest.raises(MeterAuthError) as e:
        meter.chat(build, BODY, role="code")
    assert e.value.status == 401 and meter.calls == []


def test_unix_socket_round_trip(meter, tmp_path):
    sock = tmp_path / "run" / "meter.sock"
    sock.parent.mkdir()
    sock.write_text("stale")
    meter.serve(sock)
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700 and stat.S_IMODE(sock.stat().st_mode) == 0o600
    run = meter.grant("run", parent=None, cap_usd=None)
    build = meter.grant("build", parent=run, cap_usd=None)

    def post(token, role="code"):
        return llm.post_json(BODY, unix_socket=str(sock), path="/v1/chat/completions", timeout=10,
                             headers={"Authorization": f"Bearer {token}", "X-Taltempla-Role": role,
                                      "X-Taltempla-Tool": "fetch_page"})

    resp = post(build)
    assert llm.text(resp) == "hi" and resp["x_meter"]["cost_usd"] > 0
    assert meter.calls[-1]["max_tokens"] == 32768
    assert meter._db.execute("SELECT role, tool, build_id IS NOT NULL FROM calls").fetchone() == ("code", "fetch_page", 1)
    post(build, role="main")  # the container cannot claim the main role
    assert meter.calls[-1]["max_tokens"] == 8192
    for token, code in [("g-bad", 401), (meter.session_grant, 401),
                        (meter.grant("tool_run", parent=run, cap_usd=0.00001), 402)]:
        with pytest.raises(llm.LLMError) as e:
            post(token)
        assert e.value.status == code


def test_fit_clamp_shrinks_max_tokens_then_refuses_below_the_floor(meter):
    flat = {"hit": 0.0, "miss": 0.0, "out": 1.0}  # USD 1 per 1M output tokens, no prompt cost, no peak
    meter.prices = {"deepseek-flash": {"peak": flat, "offpeak": flat}}
    run = meter.grant("run", parent=None, cap_usd=None)

    def max_tokens(cap, role, want=None):
        g = meter.grant("tool_run" if role == "tool" else "build", parent=run, cap_usd=cap)
        body = BODY if want is None else {**BODY, "max_tokens": want}
        resp = meter.chat(g, body, role=role)
        assert resp["x_meter"]["max_tokens"] == meter.calls[-1]["max_tokens"]
        return meter.calls[-1]["max_tokens"]

    assert max_tokens(1.0, "code") == 32768                 # it fits: the role clamp
    assert 9000 < max_tokens(0.01, "code") < 10000          # USD 0.01 fits about 10000 tokens: shrink, no refusal
    assert 1024 <= max_tokens(0.0015, "tool") < 1500        # role tool: floor 1024
    assert max_tokens(0.0001, "tool", want=50) == 50        # a small request is never raised
    used = meter._db.execute("SELECT max_tokens FROM calls WHERE status = 'ok' ORDER BY id").fetchall()
    assert [r[0] for r in used] == [b["max_tokens"] for b in meter.calls]  # the ledger keeps the max_tokens used
    for cap, role in ((0.003, "code"), (0.0005, "tool")):   # below the floor (4096 / 1024 tokens): 402
        n = len(meter.calls)
        with pytest.raises(MeterRefused) as e:
            max_tokens(cap, role)
        assert e.value.info["cap"] in ("build", "tool_run") and len(meter.calls) == n
    assert ledger(meter)[-1][0] == "refused"


def _flat(meter):
    flat = {"hit": 0.0, "miss": 0.0, "out": 1.0}  # USD 1 per 1M output tokens, no prompt cost
    meter.prices = {"deepseek-flash": {"peak": flat, "offpeak": flat}}


def test_waits_for_in_flight_reserves_instead_of_shrinking_or_refusing(meter):
    import threading
    import time
    _flat(meter)
    gate, fake, hold = threading.Event(), meter.upstream, []

    def slow(body, **kw):
        if hold:  # hold the first call of each race in flight
            hold.pop()
            gate.wait(10)
        return fake(body, **kw)
    meter.upstream = slow
    run = meter.grant("run", parent=None, cap_usd=None)

    def race(cap, *, revoke=False, wait_s=600.0):
        gate.clear()
        hold.append(1)
        build = meter.grant("build", parent=run, cap_usd=cap)
        out = {}

        def call(key):
            try:
                out[key] = meter.chat(build, BODY, role="tests", wait_s=wait_s)["x_meter"]["max_tokens"]
            except Exception as e:  # noqa: BLE001
                out[key] = type(e).__name__
        first = threading.Thread(target=call, args=("first",))
        first.start()
        for _ in range(500):
            if meter._grants[build].reserved:
                break
            time.sleep(0.01)
        second = threading.Thread(target=call, args=("second",))
        second.start()
        time.sleep(0.3)
        meter.revoke(build) if revoke else None
        second.join(5 if revoke else 0.01)
        gate.set()
        first.join(5), second.join(5)
        return out

    # 0.04 holds one 32768-token reserve (0.0328): the second call waits for it, then gets the full clamp
    assert race(0.04) == {"first": 32768, "second": 32768}
    assert race(0.04, revoke=True)["second"] == "MeterAuthError"  # a revoke wakes the waiter: 401
    assert 4096 <= race(0.04, wait_s=0.1)["second"] < 8000  # the wait timed out: today's clamp


def test_topup_raises_clamped_and_socket_reads_the_last_decision(meter, tmp_path):
    sock = tmp_path / "meter.sock"
    meter.serve(sock)
    run = meter.grant("run", parent=None, cap_usd=1.0)
    build = meter.grant("build", parent=run, cap_usd=0.5)

    def poll(token):
        return llm.post_json({}, unix_socket=str(sock), path="/v1/topup", timeout=10,
                             headers={"Authorization": f"Bearer {token}"})
    assert poll(build) == {"decision": "pending"}
    d = meter.topup(build, usd=5.0, seconds=60)  # the run has only 0.5 more than the build: clamp
    assert meter.grant_info(build)["cap_usd"] == pytest.approx(1.0)
    assert d == poll(build) == poll(build) == {"decision": "raised", "usd": 0.5, "seconds": 60, "seq": 1}  # kept
    meter.topup(build, usd=0.5)  # no room left and no seconds: denied, the cap stays
    assert poll(build) == {"decision": "denied", "seq": 2} and meter.grant_info(build)["cap_usd"] == pytest.approx(1.0)
    meter.topup(build, usd="junk", deny=True)
    assert poll(build) == {"decision": "denied", "seq": 3}
    meter.revoke(build)
    assert meter.topup(build, usd=1.0) is None  # revoked / unknown grant: a no-op
    meter.topup("g-nope", usd=1.0)
    for token in (build, "g-nope", meter.session_grant):
        with pytest.raises(llm.LLMError) as e:
            poll(token)
        assert e.value.status == 401
