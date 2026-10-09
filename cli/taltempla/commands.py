"""Slash commands (interactive) and the same reports for the Makefile subcommands."""

import json
import sqlite3
from pathlib import Path

from rich import box
from rich.table import Table

from . import ui
from .approvals import Approvals
from .shed_client import ShedClient, ShedError

HELP = """\
/shed                 the tool registry (name, grade, version, uses, build $, runs, fails)
/cost                 spend by run, role and tool; saved by reuse; DeepSeek balance change
/history NAME         versions and events of a tool
/rollback NAME [V]    make version V (default: the previous one) active
/allow                list "always allow" approvals;  /allow revoke NAME|HASH
/help  /exit          (also: Ctrl-D, Ctrl-C on an empty line, exit, quit)"""


def _table(title: str, cols: list[str], rows: list[list]) -> Table:
    t = Table(title=title, box=box.SIMPLE_HEAD, title_justify="left", title_style="bold")
    for c in cols:
        t.add_column(
            c, justify="right" if c.endswith("$") or c in ("v", "runs", "fails", "calls", "tok") else "left"
        )
    for r in rows:
        t.add_row(*[str(x) for x in r])
    return t


def shed_report(shed: ShedClient) -> None:
    tools = sorted(shed.tools(), key=lambda t: (t.get("grade") != "big", t["name"]))
    if not tools:
        ui.info("registry is empty (no tools yet)", "yellow")
        return
    rows = [
        [
            t["name"],
            t.get("grade", ""),
            t.get("version", ""),
            ", ".join(t.get("uses") or []) or "-",
            f"{t.get('build_cost_usd') or 0:.4f}",
            t.get("invocations", 0),
            t.get("failures", 0),
            ui.short(t.get("summary", ""), 60),
        ]
        for t in tools
    ]
    ui.console.print(
        _table(
            f"toolshed · {len(tools)} tools",
            ["name", "grade", "v", "uses", "build $", "runs", "fails", "summary"],
            rows,
        )
    )


def cost_report(ledger: Path, session_id: str | None = None, balance_now: float | None = None) -> None:
    from .meter import report

    r = report(Path(ledger), session_id)
    t = r["total"]
    if not t["calls"]:
        ui.info("no LLM calls in the ledger yet", "yellow")
    scope = f"session {session_id}" if session_id else "all sessions"

    def agg(title: str, key: str, rows: list[dict]) -> None:
        if rows:
            ui.console.print(
                _table(
                    title,
                    [key, "calls", "hit", "miss", "out", "$"],
                    [
                        [
                            x[key],
                            x["calls"],
                            ui.tok(x["hit"] or 0),
                            ui.tok(x["miss"] or 0),
                            ui.tok(x["out"] or 0),
                            f"{x['cost_usd'] or 0:.4f}",
                        ]
                        for x in rows
                    ],
                )
            )

    if not session_id:
        agg("by session", "session_id", r["by_session"])
    agg(f"by run · {scope}", "run_id", r["by_run"][-12:])
    agg("by role", "role", r["by_role"])
    agg("by tool (LLM spend of agentic tools)", "tool", r["by_tool"])
    agg("by build (Big Chef)", "build_id", r["by_build"])
    ui.info(
        f"total ${t['cost_usd']:.4f} in {t['calls']} calls ({t['refused']} refused, {t['errors']} errors) · "
        f"{ui.tok(t['hit'] + t['miss'] + t['out'])} tok {t['cache_pct']:.0f}% cached · "
        f"saved by reuse ${r['saved']['total_usd']:.4f}",
        "bold",
    )
    if r["saved"]["by_tool"]:
        ui.dim(
            "  reused: "
            + ", ".join(f"{x['tool']} v{x['version']} ${x['saved_usd']:.4f}" for x in r["saved"]["by_tool"])
        )
    b = r.get("balance") or {}
    end = b.get("end") if b.get("end") is not None else balance_now
    if b.get("start") is not None and end is not None:
        ui.info(
            f"DeepSeek balance ${b['start']:.4f} → ${end:.4f} (change ${b['start'] - end:.4f}; "
            f"ledger ${t['cost_usd']:.4f})",
            "dim",
        )
    elif not session_id and Path(ledger).exists():
        db = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        rows = db.execute(
            "SELECT balance_start, balance_end FROM sessions WHERE balance_start IS NOT NULL "
            "AND balance_end IS NOT NULL ORDER BY started"
        ).fetchall()
        db.close()
        if rows:
            ui.info(
                f"DeepSeek balance ${rows[0][0]:.4f} (first session start) → ${rows[-1][1]:.4f} (last end), "
                f"change ${rows[0][0] - rows[-1][1]:.4f}; ledger ${t['cost_usd']:.4f}",
                "dim",
            )


def history_report(shed: ShedClient, name: str) -> None:
    h = shed.history(name)
    vcols = [
        "version",
        "content_hash",
        "perm_hash",
        "parent",
        "origin",
        "build_cost_usd",
        "created_at",
        "active",
    ]
    vs = h.get("versions") or []
    cols = [c for c in vcols if any(c in v for v in vs)]
    ui.console.print(_table(f"{name} · versions", cols, [[_cell(v.get(c)) for c in cols] for v in vs]))
    ev = h.get("events") or []
    rows = [
        [
            _cell(e.get("ts") or e.get("created_at")),
            e.get("kind", ""),
            _cell(e.get("version")),
            ui.short(json.dumps(e.get("data", {}), ensure_ascii=False), 90),
        ]
        for e in ev[-30:]
    ]
    ui.console.print(_table("events (latest 30)", ["ts", "kind", "v", "data"], rows))


def _cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, str) and len(v) == 64:
        return v[:12]
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)[:24]


def rollback(shed: ShedClient, name: str, version: int | None) -> None:
    r = shed.rollback(name, version)
    ui.ok(f"{r.get('name', name)}: active version is now v{r.get('active_version')}")


def allow(approvals: Approvals, args: list[str]) -> None:
    if args[:1] == ["revoke"] and len(args) > 1:
        gone = approvals.revoke(args[1])
        ui.info(
            f"revoked {len(gone)}: " + ", ".join(f"{g['name']} v{g['version']}" for g in gone)
            if gone
            else f"no approval matches {args[1]!r}",
            "yellow",
        )
        return
    rows = [
        [a["name"], a.get("version", "-"), a["perm_hash"][:12], a.get("granted_at", "")]
        for a in approvals.entries()
    ]
    if not rows:
        ui.info('no "always allow" approvals', "dim")
        return
    ui.console.print(_table("always allow (by permission hash)", ["tool", "v", "perm hash", "granted"], rows))
    ui.dim("revoke: /allow revoke NAME|HASH")


def dispatch(line: str, ctx) -> bool:
    """Run one slash command. Return False to exit. ctx has shed, approvals, ledger, meter, agent, session_id."""
    parts = line.strip().split()
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd in ("/exit", "/quit", "/q"):
            return False
        if cmd == "/help":
            ui.info(HELP)
        elif cmd == "/allow":
            allow(ctx.approvals, args)
        elif cmd == "/cost":
            cost_report(ctx.ledger, ctx.session_id, ctx.meter.balance() if ctx.meter else None)
        elif cmd in ("/shed", "/history", "/rollback") and ctx.shed is None:
            ui.warn("toolshed offline (chat-only mode)")
        elif cmd == "/shed":
            shed_report(ctx.shed)
        elif cmd == "/history" and args:
            history_report(ctx.shed, args[0])
        elif cmd == "/rollback" and args:
            rollback(ctx.shed, args[0], int(args[1].lstrip("vV")) if len(args) > 1 else None)
        else:
            ui.warn(f"unknown command or missing argument: {line.strip()}  (/help)")
    except ShedError as e:
        ui.error(str(e))
    except ValueError as e:
        ui.error(f"bad argument: {e}")
    return True
