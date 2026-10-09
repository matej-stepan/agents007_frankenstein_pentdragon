"""Operator gates: the install gate after a Chef handoff, and the use gate before each use_tool."""

import json
import math
import os
import sys

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import ui
from .shed_client import ShedClient, ShedError


def mode() -> str:
    return os.environ.get("TALTEMPLA_GATE", "ask").strip().lower()


def ask(message: str, options: list[tuple[str, str]], auto: str) -> str:
    """Return the chosen value. auto mode picks `auto`; Ctrl-C/EOF picks the last option (the safe one)."""
    labels = dict(options)
    if mode() == "auto":
        ui.info(f"  gate (auto): {labels[auto]}", "cyan")
        return auto
    safe = options[-1][0]
    if ui.plain or not sys.stdin.isatty():
        ui.info(message, "bold")
        for i, (_, label) in enumerate(options, 1):
            print(f"  {i}) {label}")
        try:
            raw = input("choice> ").strip()
        except (EOFError, KeyboardInterrupt, OSError):  # OSError: no usable stdin
            return safe
        return options[int(raw) - 1][0] if raw.isdigit() and 1 <= int(raw) <= len(options) else safe
    from prompt_toolkit.shortcuts import choice

    try:
        return choice(message=message, options=options, default=options[0][0])
    except (KeyboardInterrupt, EOFError):
        return safe


def perms_text(p: dict | None) -> str:
    p = p or {}
    parts = ["net" if p.get("network") else "no net"]
    if p.get("llm_usd"):
        parts.append(f"LLM ≤ ${p['llm_usd']:.2f}")
    if p.get("files", "none") != "none":
        parts.append(f"files {p['files']}")
    return " · ".join(parts)


def render_handoff(h: dict) -> None:
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True)
    t.add_column(overflow="fold")
    for row in h.get("tree", []):
        new = row.get("status") == "new"
        entry = row["name"] == h.get("entry")
        mark = Text("new", "bold green") if new else Text("reused", "dim")
        name = Text(row["name"] + ("" if new else f" v{row.get('version', '?')}"), "bold" if entry else "")
        name.append(f" {row.get('grade', '')}" + (" (entry)" if entry else ""), "dim")
        facts = [perms_text(row.get("permissions"))]
        if row.get("uses"):
            facts.insert(0, "chains " + ", ".join(row["uses"]))
        if row.get("deps"):
            facts.append("deps " + ", ".join(row["deps"]))
        if new:
            facts.append(
                f"tests {row.get('tests', '-')} · {row.get('iterations', '-')} iter · "
                f"review {row.get('verdict', '-')} · ${row.get('cost_usd', 0):.4f}"
            )
        t.add_row(mark, name, " · ".join(facts))
    body = [t, Text(f"build cost ${h.get('cost_usd', 0):.4f}", "dim")]
    for w in h.get("warnings") or []:
        body.append(Text(f"warning: {w}", "yellow"))
    title = f"Install {h.get('entry')}? (build {h.get('build_id')})"
    ui.console.print(Panel(_stack(body), title=title, title_align="left", border_style="cyan", expand=False))


def _stack(items) -> Table:
    g = Table.grid()
    for it in items:
        g.add_row(it)
    return g


def show_drafts(shed: ShedClient, h: dict) -> None:
    parts = []
    for row in h.get("tree", []):
        if row.get("status") != "new" or not row.get("content_hash"):
            continue
        try:
            d = shed.draft(row["content_hash"])
        except ShedError as e:
            parts.append(Text(f"{row['name']}: {e}", "red"))
            continue
        files = d.get("files") or {}
        parts.append(Text(f"\n══ {row['name']} · {row['content_hash'][:12]} ══", "bold cyan"))
        parts.append(
            Syntax(json.dumps(d.get("manifest"), indent=2, ensure_ascii=False), "json", word_wrap=True)
        )
        for fn, lexer in (("tool.py", "python"), ("test_tool.py", "python")):
            parts.append(Text(f"── {fn}", "bold"))
            parts.append(Syntax(files.get(fn, ""), lexer, line_numbers=True, word_wrap=True))
        parts.append(Text("── SKILL.md", "bold"))
        parts.append(Markdown(files.get("SKILL.md", "")))
        for tr in d.get("test_runs") or []:
            parts.append(
                Text(
                    f"── test run {tr.get('kind', '')}: {tr.get('passed')} passed, {tr.get('failed')} failed",
                    "bold",
                )
            )
            parts.append(Text(str(tr.get("log", ""))[-3000:], "dim"))
        rv = d.get("review") or {}
        parts.append(Text(f"── review: {rv.get('verdict', '-')}", "bold"))
        parts.append(Text(str(rv.get("report", ""))[:3000]))
    if ui.plain or not sys.stdout.isatty():
        for p in parts:
            ui.console.print(p)
        return
    with ui.console.pager(styles=True):
        for p in parts:
            ui.console.print(p)


def install(shed: ShedClient, h: dict) -> str:
    """Install gate. Returns 'once', 'always' or 'reject'."""
    opts = [
        ("once", "Install + run once"),
        ("always", "Install + always allow"),
        ("show", "Show code·tests·log"),
        ("reject", "Reject"),
    ]
    while True:
        render_handoff(h)
        pick = ask(f"Install {h.get('entry')}?", opts, auto="once")
        if pick != "show":
            return pick
        show_drafts(shed, h)


def use(tool: dict) -> str:
    """Use gate. Returns 'once', 'always' or 'deny'."""
    uses = tool.get("uses") or []
    chain = f" chains {', '.join(uses)} ·" if uses else ""
    msg = f"Run {tool['name']} v{tool.get('version', '?')}?{chain} {perms_text(tool.get('permissions'))}"
    return ask(msg, [("once", "Allow once"), ("always", "Always allow"), ("deny", "Deny")], auto="once")


# ---- build options (G2): the operator picks the cap, the effort and the wall time of each Chef build ----------
EFFORTS = ("low", "high", "max")
BUILD_DEFAULTS = {"cap_usd": 1.00, "effort": None, "cap_seconds": 360, "plan_seconds": 150}  # effort None = roles


def _num(v, default, cast=float):
    try:
        f = float(v)
        return cast(f) if math.isfinite(f) else default
    except (TypeError, ValueError, OverflowError):
        return default


def clamp_options(d: dict | None, max_cap: float) -> dict:
    """Lenient: a bad value falls back to the default, never raises. cap_usd in (0, max_cap], minutes in [1, 60],
    plan_seconds in [30, cap_seconds); a plan time at or over the wall time gets the default share (150/360), else the
    P1 last-turn nudge never fires. effort None (or not low/high/max) = the server's role defaults (D58)."""
    d = d if isinstance(d, dict) else {}
    cap = _num(d.get("cap_usd"), BUILD_DEFAULTS["cap_usd"])
    cap = min(cap if cap > 0 else BUILD_DEFAULTS["cap_usd"], max(_num(max_cap, 0.0), 0.0))
    effort = d.get("effort") if d.get("effort") in EFFORTS else None
    secs = min(max(_num(d.get("cap_seconds"), BUILD_DEFAULTS["cap_seconds"], int), 60), 3600)
    plan = max(_num(d.get("plan_seconds"), BUILD_DEFAULTS["plan_seconds"], int), 30)
    if plan >= secs:
        plan = min(max(round(secs * BUILD_DEFAULTS["plan_seconds"] / BUILD_DEFAULTS["cap_seconds"]), 30), secs)
    return {"cap_usd": round(cap, 4), "effort": effort, "cap_seconds": secs, "plan_seconds": plan}


def options_text(o: dict) -> str:
    return f"cap ${o['cap_usd']:.2f} · effort {o['effort'] or 'roles'} · {o['cap_seconds'] / 60:g} min"


def _read(label: str) -> str | None:
    """One line of text; None on Ctrl-C/EOF."""
    try:
        if ui.plain or not sys.stdin.isatty():
            return input(label)
        from prompt_toolkit import prompt

        return prompt(label)
    except (EOFError, KeyboardInterrupt, OSError):
        return None


def build_options(defaults: dict, what: str, max_cap: float) -> dict | None:
    """G2 build gate. Returns {cap_usd, effort, cap_seconds, plan_seconds}, or None = the operator cancelled.
    auto mode: the defaults. Ctrl-C/EOF anywhere: cancel (the safe choice: no spend)."""
    o = clamp_options(defaults, max_cap)
    while True:
        opts = [("start", f"Start: {options_text(o)}"), ("change", "Change cap, effort or time"),
                ("cancel", "Cancel the build")]
        pick = ask(f"Build {what}? (run budget left ${max_cap:.2f})", opts, auto="start")
        if pick != "change":
            return o if pick == "start" else None
        n = dict(o)
        raw = _read(f"cap USD [{o['cap_usd']:.2f}, max {max_cap:.2f}]: ")
        if raw is None:
            return None
        n["cap_usd"] = _num(raw.strip().lstrip("$"), o["cap_usd"]) if raw.strip() else o["cap_usd"]
        cur = o["effort"] or "roles"  # keep first (the Enter default); cancel last (the Ctrl-C/EOF pick)
        eff = ask("DeepSeek effort for every Chef role", [("keep", f"{cur} (keep)")] + [
            (e, "roles (CHEF_<ROLE>_EFFORT defaults)" if e == "roles" else e) for e in ("roles", *EFFORTS) if e != cur
        ] + [("cancel", "Cancel the build")], auto="keep")
        if eff == "cancel":
            return None
        n["effort"] = o["effort"] if eff == "keep" else (None if eff == "roles" else eff)
        raw = _read(f"minutes [{o['cap_seconds'] / 60:g}, 1-60]: ")
        if raw is None:
            return None
        mins = _num(raw, 0.0) if raw.strip() else 0.0
        if mins > 0:  # P1 keeps its share of the wall time
            n["cap_seconds"] = int(min(max(mins, 1), 60) * 60)
            n["plan_seconds"] = round(o["plan_seconds"] * n["cap_seconds"] / max(o["cap_seconds"], 1))
        o = clamp_options(n, max_cap)
