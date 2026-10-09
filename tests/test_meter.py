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
    assert meter.calls[0]["max_tokens"] == 8192 and meter.calls[0]["model"] == "deepseek-flash"
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
    assert meter.calls[-1]["max_tokens"] == 4096
    for token, code in [("g-bad", 401), (meter.session_grant, 401),
                        (meter.grant("tool_run", parent=run, cap_usd=0.00001), 402)]:
        with pytest.raises(llm.LLMError) as e:
            post(token)
        assert e.value.status == code
