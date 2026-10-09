"""Rich console helpers and the status line."""

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

console = Console(highlight=False)
plain = False  # --once: no markdown rendering, no spinners


def set_plain(on: bool) -> None:
    global plain
    plain = on
    console.soft_wrap = on  # headless logs: one line per trace, no wrap at 80 columns


def dim(msg: str) -> None:
    console.print(Text(msg, style="dim"))


def info(msg: str, style: str = "") -> None:
    console.print(Text(msg, style=style))


def ok(msg: str) -> None:
    console.print(Text(msg, style="green"))


def warn(msg: str) -> None:
    console.print(Text(msg, style="yellow"))


def error(msg: str) -> None:
    console.print(Text(msg, style="bold red"))


def answer(text: str) -> None:
    if plain:
        print(text, flush=True)
    else:
        console.print(Markdown(text or "_(no answer)_"))


def spinner(msg: str):
    """Context manager: a spinner in interactive mode, nothing in plain mode."""
    if plain:
        from contextlib import nullcontext

        return nullcontext()
    return console.status(Text(msg, style="dim"), spinner="dots")


def short(obj, n: int = 80) -> str:
    s = obj if isinstance(obj, str) else repr(obj)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tok(n: float) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else f"{int(n)}"


def status_line(st: dict) -> str:
    """st = Meter.status(): session_usd, run_usd, tokens, cache_pct (0-100), saved_usd, cap_left_usd."""
    cache, saved_usd = st.get("cache_pct", 0) or 0, st.get("saved_usd", 0) or 0
    return (
        f"${st.get('session_usd', 0):.4f} sess · run ${st.get('run_usd', 0):.4f} · "
        f"{tok(st.get('tokens', 0) or 0)} tok {cache:.0f}% cached · saved ${saved_usd:.2f} · "
        f"cap left ${st.get('cap_left_usd', 0) or 0:.2f}"
    )
