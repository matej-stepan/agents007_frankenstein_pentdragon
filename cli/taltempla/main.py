"""Entry point `taltempla`: the interactive CLI, `--once PROMPT` headless mode and the Makefile subcommands."""

import argparse
import os
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from . import commands, ui
from .approvals import Approvals
from .shed_client import ShedClient, ShedError

PKG = Path(__file__).resolve().parent
SHED_HINT = "toolshed is down. run: make toolshed-up"
SUBCOMMANDS = ("shed", "cost", "history", "rollback", "allow")
EXIT_WORDS = {"exit", "quit", "exit()", "quit()", ":q", ":wq", "q", "bye"}


def is_exit_word(line: str) -> bool:
    """A bare exit word is /exit; it never goes to the model."""
    return line.strip().lower() in EXIT_WORDS


def project_root() -> Path:
    if os.environ.get("TALTEMPLA_HOME"):
        return Path(os.environ["TALTEMPLA_HOME"]).resolve()
    for p in (Path.cwd(), *Path.cwd().parents):
        if (p / "contract").is_dir() and (p / "cli").is_dir():
            return p
    return PKG.parents[1]


def read_env(path: Path) -> dict:
    """Tiny KEY=VALUE parser. The values stay in this dict; they never go to os.environ."""
    out = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def caps_from_env() -> dict:
    d = {"build": 1.00, "run": 3.00, "session": 6.00, "total": 20.00}  # meter.DEFAULT_CAPS (D54)
    return {k: float(os.environ.get(f"TALTEMPLA_CAP_{k.upper()}") or v) for k, v in d.items()}


def build_defaults_from_env(caps: dict) -> dict:
    """G2/G3 defaults of each Chef build. Lenient: a bad value keeps the default (gate.clamp_options clamps)."""
    d = {"cap_usd": caps.get("build", 1.00), "effort": None, "cap_seconds": 360, "plan_seconds": 150}  # None = roles
    effort = (os.environ.get("TALTEMPLA_BUILD_EFFORT") or "").strip().lower()
    if effort in ("low", "high", "max"):
        d["effort"] = effort
    for key, env in (("cap_seconds", "TALTEMPLA_BUILD_SECONDS"), ("plan_seconds", "TALTEMPLA_PLAN_SECONDS")):
        try:
            d[key] = int(float(os.environ.get(env) or d[key]))
        except (TypeError, ValueError, OverflowError):
            ui.warn(f"{env} is not a number; using {d[key]}")
    return d


def shed_alive(shed: ShedClient) -> dict | None:
    try:
        h = shed.health()
        return h if h.get("ok") else None
    except (ShedError, ValueError):
        return None


def subcommand(cmd: str, rest: list[str], frank: Path) -> int:
    if cmd == "cost":
        commands.cost_report(frank / "ledger.db")
        return 0
    if cmd == "allow":
        commands.allow(Approvals(frank / "permissions.json"), rest)
        return 0
    shed = ShedClient(frank / "admin.token")
    if not shed_alive(shed):
        ui.error(SHED_HINT)
        return 2
    try:
        if cmd == "shed":
            commands.shed_report(shed)
        elif cmd == "history" and rest:
            commands.history_report(shed, rest[0])
        elif cmd == "rollback" and rest:
            commands.rollback(shed, rest[0], int(rest[1].lstrip("vV")) if len(rest) > 1 and rest[1] else None)
        else:
            ui.error(f"usage: taltempla {cmd} NAME" + (" [VERSION]" if cmd == "rollback" else ""))
            return 1
    except ShedError as e:
        ui.error(str(e))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="taltempla", description="Taltempla: an agent that builds its own tools."
    )
    ap.add_argument("--once", metavar="PROMPT", help="run one prompt headless and exit (0 = answered)")
    ap.add_argument("cmd", nargs="?", help=f"subcommand: {', '.join(SUBCOMMANDS)}")
    ap.add_argument("rest", nargs="*", help="subcommand arguments")
    a = ap.parse_args(argv)
    root = project_root()
    frank = root / ".frank"
    frank.mkdir(mode=0o700, exist_ok=True)
    if a.cmd:
        if a.cmd not in SUBCOMMANDS:
            ap.error(f"unknown subcommand {a.cmd!r}")
        return subcommand(a.cmd, a.rest, frank)

    ui.set_plain(a.once is not None)
    no_shed = os.environ.get("TALTEMPLA_NO_SHED") == "1"
    shed = None if no_shed else ShedClient(frank / "admin.token")
    health = None if no_shed else shed_alive(shed)
    if shed is not None and health is None:
        ui.error(SHED_HINT)
        return 2

    env = read_env(root / ".env")
    key = env.get("OAI_COMPATIBLE_KEY") or os.environ.get("OAI_COMPATIBLE_KEY")
    if not key:
        ui.error("OAI_COMPATIBLE_KEY is missing in .env. Ask the operator to add it.")
        return 2
    base_url = (
        env.get("OAI_COMPATIBLE_BASE_URL")
        or os.environ.get("OAI_COMPATIBLE_BASE_URL")
        or "https://api.deepseek.com"
    )
    from .loop import Agent
    from .meter import Meter

    caps = caps_from_env()
    model = os.environ.get("TALTEMPLA_MODEL") or "deepseek-v4-pro"
    session_id = time.strftime("s%Y%m%d-%H%M%S-", time.gmtime()) + secrets.token_hex(2)
    run_dir = frank / "run"
    run_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(run_dir, 0o700)
    meter = Meter(
        ledger_path=frank / "ledger.db",
        prices_path=PKG / "prices.json",
        key=key,
        base_url=base_url,
        caps=caps,
        session_id=session_id,
        model=model,
    )
    del key, env
    meter.serve(run_dir / "meter.sock")
    sgrant = meter.session_grant
    meter.record_balance("start")
    approvals = Approvals(frank / "permissions.json")
    agent = Agent(
        meter=meter,
        shed=shed,
        session_grant=sgrant,
        session_id=session_id,
        root=root,
        approvals=approvals,
        model=model,
        caps=caps,
        effort=os.environ.get("TALTEMPLA_EFFORT") or "high",
        build_defaults=build_defaults_from_env(caps),
    )
    status = lambda: ui.status_line(meter.status())
    try:
        if a.once is not None:
            ok = agent.run(a.once)
            ui.dim(status())
            return 0 if ok else 1
        if shed is not None and not (shed_alive(shed) or {}).get("meter"):
            ui.warn(
                "the toolshed does not see the meter socket (.frank/run/meter.sock); tool LLM calls will fail"
            )
        ctx = SimpleNamespace(
            shed=shed,
            approvals=approvals,
            ledger=frank / "ledger.db",
            meter=meter,
            agent=agent,
            session_id=session_id,
        )
        shed_info = "chat-only (no toolshed)" if shed is None else f"toolshed {health.get('tools', 0)} tools"
        ui.info(
            f"Taltempla · {model} · session {session_id} · {shed_info} · caps run ${caps['run']:.2f} "
            f"session ${caps['session']:.2f} · /help · Ctrl-D or /exit to quit",
            "bold cyan",
        )
        interactive(agent, ctx, frank / "history", status)
        return 0
    finally:
        meter.record_balance("end")
        meter.revoke(sgrant)
        if a.once is None:
            ui.dim("bye · " + status())
        meter.close()


def ctrl_c_bindings():
    """Ctrl-C at the prompt: an empty line exits, a line with text is cleared, a second Ctrl-C within 2 s exits."""
    from prompt_toolkit.key_binding import KeyBindings

    kb, last = KeyBindings(), [0.0]

    @kb.add("c-c")
    def _(event):
        buf, now = event.app.current_buffer, time.monotonic()
        if not buf.text.strip() or now - last[0] < 2:
            event.app.exit(exception=EOFError)
        else:
            last[0] = now
            buf.reset()

    return kb


def interactive(agent, ctx, history_path: Path, status) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    line_cache = [status()]
    session = PromptSession(
        history=FileHistory(str(history_path)),
        bottom_toolbar=lambda: " " + line_cache[0],
        key_bindings=ctrl_c_bindings(),
    )
    while True:
        try:
            line = session.prompt("› ").strip()
        except (EOFError, KeyboardInterrupt):  # Ctrl-D, or Ctrl-C (see ctrl_c_bindings)
            break
        if not line:
            continue
        if is_exit_word(line):
            break
        if line.startswith("/"):
            if not commands.dispatch(line, ctx):
                break
        else:
            try:
                agent.run(line)  # Ctrl-C ends the run (loop.py) and returns here
            except KeyboardInterrupt:  # a second Ctrl-C during the run's cleanup: quit
                break
            ui.dim(status())
        line_cache[0] = status()


if __name__ == "__main__":
    sys.exit(main())
