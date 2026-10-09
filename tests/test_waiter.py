"""The Waiter (G1): robust JSON parse; any error falls back to the raw reason (it never stops a run)."""

from taltempla import waiter
from taltempla.meter import MeterRefused


class RefusingMeter:
    def chat(self, *a, **k):
        raise MeterRefused({"cap": "run"})


def test_parse_and_fallback():
    d = waiter.parse('ok:\n```json\n{"cause": "a\\nb\\nc\\nd\\ne", "advice": "' + "x" * 2000 + '"}\n```')
    assert d["cause"] == "a\nb\nc\nd" and len(d["advice"]) == waiter.MAX_ADVICE
    assert waiter.parse("no json {bad") is None and waiter.parse('{"advice": "x"}') is None
    events = [{"type": "trace", "msg": "m" * 50_000}, "junk", {"type": "test_run"}]
    assert len(waiter.build_text(events, "boom")) <= waiter.MAX_INPUT + 100
    d = waiter.diagnose(RefusingMeter(), "g", "m", events, "boom")
    assert d == {"cause": "boom", "advice": "", "replan": False, "ok": False}


def test_replan_and_checkpoint_names():
    assert waiter.parse('{"cause": "c", "replan": true}')["replan"] is True
    assert waiter.parse('{"cause": "c"}')["replan"] is False
    events = [{"type": "checkpoint", "stage": "plan", "plan": {"secret_plan_field": 1}},
              {"type": "checkpoint", "stage": "tool", "tool": "fetch_x", "content_hash": "h"}]
    text = waiter.build_text(events, "boom")
    assert "[checkpoint] plan saved" in text and "fetch_x green" in text and "secret_plan_field" not in text
