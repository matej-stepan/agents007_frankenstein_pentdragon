"""Rich console helpers and the status line."""

import contextlib
import time

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


class ChefStatus:
    """The chef spinner of one build stream (interactive mode only). Its text is computed at each refresh (__rich__).
    rich allows ONE live display: paused() around a gate prompt; `with` stops it on every exit path (Ctrl-C too).
    Any rich error = no spinner (it never stops a build)."""

    def __init__(self, cap_s: int):
        self.phase, self.tool, self.cost, self.cap_s = "start", "", 0.0, int(cap_s or 0)
        self.t0 = self.last = time.monotonic()
        self._st = None

    def on(self, ev: dict) -> None:
        """One stream event: a new step (trace lines carry the phase and the build cost so far)."""
        self.last = time.monotonic()
        if ev.get("type") == "trace":
            self.phase = str(ev.get("phase") or self.phase)
            with contextlib.suppress(TypeError, ValueError):
                self.cost = float(ev.get("cost_usd") or self.cost)
        if ev.get("tool") is not None or ev.get("type") == "trace":
            self.tool = str(ev.get("tool") or "")

    def sync(self, elapsed_s, cap_s) -> None:
        """The server's clock (a cap_hit event): the operator's wait does not count on the server either."""
        with contextlib.suppress(TypeError, ValueError):
            self.t0, self.cap_s = time.monotonic() - float(elapsed_s), int(cap_s) or self.cap_s

    def __rich__(self) -> Text:
        now = time.monotonic()
        tool = f" {self.tool}" if self.tool else ""
        return Text(f"chef {self.phase}{tool} · {now - self.last:.0f} s since the last step · ${self.cost:.4f} · "
                    f"{(now - self.t0) / 60:.1f}/{self.cap_s / 60:g} min", style="dim")

    def start(self) -> None:
        if plain or self._st is not None:
            return
        try:
            self._st = console.status(self, spinner="dots")
            self._st.start()
        except Exception:  # noqa: BLE001 - e.g. another live display is active: no spinner
            self._st = None

    def stop(self) -> None:
        st, self._st = self._st, None
        if st is not None:
            with contextlib.suppress(Exception):
                st.stop()

    @contextlib.contextmanager
    def paused(self):
        """Stop for a prompt, then restart. The pause does not count as build time (like the server's clock)."""
        was, t = self._st is not None, time.monotonic()
        self.stop()
        try:
            yield
        finally:
            d = time.monotonic() - t
            self.t0, self.last = self.t0 + d, self.last + d
            if was:
                self.start()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


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
