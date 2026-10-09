"""The Waiter (G1): after a failed Chef build, ONE metered LLM call reads the build trace and gives a short cause for
the operator and advice for the Chef's retry. Not a tool of the main agent; it never touches the agent history.
Any error -> the fallback (cause = the raw failure reason, no advice): the Waiter never stops a run."""

import json
from pathlib import Path

from . import llm, ui

PROMPT = Path(__file__).with_name("waiter.md").read_text(encoding="utf-8")
MAX_INPUT = 12_000  # chars of build text; the tail is kept (the failure is at the end)
MAX_ADVICE = 1500
KEEP_EVENTS = 200  # stream events t_big_chef keeps for the Waiter


def build_text(events, reason: str, task: str = "", need: str = "") -> str:
    """Compact text of one build: trace lines, test runs with log tails, the failure reason. Tolerates bad events."""
    lines = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "trace":
            tool = f" {ev['tool']}" if ev.get("tool") else ""
            lines.append(f"[{ev.get('phase', '?')}{tool}] {ev.get('msg', '')}")
        elif t == "test_run":
            lines.append(f"[test {ev.get('tool', '?')} {ev.get('kind', '')} #{ev.get('iteration', '?')}] "
                         f"{ev.get('passed', '?')} passed, {ev.get('failed', '?')} failed")
            if ev.get("excerpt"):
                lines.append(str(ev["excerpt"])[-1500:])
        elif t == "checkpoint":  # names only, never the plan JSON
            lines.append(f"[checkpoint] {ev.get('tool')} green (saved)" if ev.get("stage") == "tool"
                         else "[checkpoint] plan saved")
        elif t == "cap_hit":
            lines.append(f"[cap {ev.get('kind', '?')} #{ev.get('n', '?')}] ${ev.get('spent_usd', '?')} of "
                         f"${ev.get('cap_usd', '?')} · {ev.get('elapsed_s', '?')} of {ev.get('cap_s', '?')} s")
    head = f"TASK: {str(task)[:1000]}\nNEED: {str(need)[:1000]}\nBUILD TRACE:\n"
    tail = f"\nFAILED: {str(reason)[:2000]}"
    body, room = "\n".join(lines), max(MAX_INPUT - len(head) - len(tail), 1000)
    if len(body) > room:
        body = "[earlier trace cut]\n" + body[-room:]
    return head + body + tail


def parse(text: str) -> dict | None:
    """The first JSON object with a cause (code fences and prose around it are fine)."""
    dec, s, i = json.JSONDecoder(), str(text or ""), 0
    while (i := s.find("{", i)) >= 0:
        try:
            d, _ = dec.raw_decode(s, i)
        except ValueError:
            i += 1
            continue
        if isinstance(d, dict) and str(d.get("cause") or "").strip():
            cause = "\n".join(ui.short(ln, 200) for ln in str(d["cause"]).strip().splitlines()[:4])
            return {"cause": cause, "advice": str(d.get("advice") or "").strip()[:MAX_ADVICE],
                    "replan": d.get("replan") in (True, "true")}  # missing or bad = False (a resume is offered)
        i += 1
    return None


def diagnose(meter, grant: str, model: str, events, reason: str, task: str = "", need: str = "") -> dict:
    """{"cause", "advice", "replan", "ok"}. ok=False = the fallback (no paid answer, a refused reserve, bad JSON,
    Ctrl-C). replan=True: the plan itself caused the failure (no resume from the plan checkpoint)."""
    fallback = {"cause": str(reason), "advice": "", "replan": False, "ok": False}
    try:
        body = {"model": model, "reasoning_effort": "high", "max_tokens": 16384,  # reasoning counts in it (D58)
                "messages": [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": build_text(events, reason, task, need)}]}
        with ui.spinner("waiter: reading the build…"):
            resp = meter.chat(grant, body, role="waiter")
        d = parse(llm.text(resp))
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - the Waiter must never stop the run
        ui.dim(f"  waiter: no diagnosis ({type(e).__name__}: {ui.short(str(e), 120)})")
        return fallback
    if d is None:
        ui.dim("  waiter: no diagnosis (no JSON in the reply)")
        return fallback
    return {**d, "ok": True}
