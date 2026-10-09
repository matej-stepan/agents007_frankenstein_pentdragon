"""Runner: every execution of agent code goes through here (setpriv uid tool, prlimit, timeout, env -i).

Contract section 7. Only shedd (root in the container) calls it; the host never runs tool code.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

RUN_ROOT = Path("/tmp/shed-run")
RT_SOCK = "/run/shed/rt.sock"
SDK = "/opt/shed/sdk"
TOOL_UID = 1000
TIMEOUT_DEFAULT, TIMEOUT_MAX = 60, 180
MEMORY_DEFAULT, MEMORY_MAX = 2048, 4096
OUT_MAX = 16000


@dataclass
class ExecResult:
    exit: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


@dataclass
class TestResult:
    passed: int
    failed: int
    errors: int
    log: str
    duration_ms: int
    timed_out: bool


def clip(text: str, n: int = OUT_MAX) -> str:
    """Keep the head and (mostly) the tail; failures are at the end of pytest output."""
    if len(text) <= n:
        return text
    head = n // 5
    return text[:head] + f"\n...[{len(text) - n} chars cut]...\n" + text[-(n - head):]


def limits(manifest: dict) -> tuple[int, int]:
    lim = manifest.get("limits") or {}
    t = max(1, min(int(lim.get("timeout_s", TIMEOUT_DEFAULT)), TIMEOUT_MAX))
    mem = max(128, min(int(lim.get("memory_mb", MEMORY_DEFAULT)), MEMORY_MAX))
    return t, mem


def _rundir(files: dict[str, str]) -> Path:
    RUN_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    d = RUN_ROOT / secrets.token_hex(8)
    (d / "tool").mkdir(parents=True, mode=0o755)
    for name, text in files.items():
        if "/" in name or name.startswith("."):
            raise ValueError(f"bad file name {name!r}")
        p = d / "tool" / name
        p.write_text(text)
        p.chmod(0o644)
    for sub in ("home", "out"):
        (d / sub).mkdir(mode=0o700)
        os.chown(d / sub, TOOL_UID, TOOL_UID)
    d.chmod(0o755)
    return d


def _exec(cmd: list[str], d: Path, *, timeout_s: int, memory_mb: int = MEMORY_DEFAULT, token: str = "",
          sock: str = RT_SOCK) -> ExecResult:
    tool_dir = d / "tool"
    argv = ["setpriv", f"--reuid={TOOL_UID}", f"--regid={TOOL_UID}", "--clear-groups", "--no-new-privs", "--",
            "prlimit", f"--as={memory_mb * 1024 * 1024}", "--nproc=512", "--nofile=1024", "--",
            "timeout", "-k", "2", str(timeout_s),
            "env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", f"HOME={d / 'home'}", "LANG=C.UTF-8",
            "PYTHONDONTWRITEBYTECODE=1", f"PYTHONPATH={SDK}:{tool_dir}", f"SHED_SOCK={sock}",
            f"SHED_RUN_TOKEN={token}", "OPENBLAS_NUM_THREADS=2", "OMP_NUM_THREADS=2", *cmd]
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=tool_dir, capture_output=True, text=True, errors="replace",
                           timeout=timeout_s + 15, stdin=subprocess.DEVNULL, check=False)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = 124, (e.stdout or b"").decode(errors="replace"), (e.stderr or b"").decode(errors="replace")
    ms = int((time.monotonic() - t0) * 1000)
    return ExecResult(code, clip(out), clip(err), code in (124, 137), ms)


def probe(code: str, files: dict[str, str] | None = None, timeout_s: int = 30, *, memory_mb: int = MEMORY_DEFAULT
          ) -> ExecResult:
    d = _rundir({**(files or {}), "_probe.py": code})
    try:
        return _exec(["python3", "_probe.py"], d, timeout_s=max(1, min(timeout_s, TIMEOUT_MAX)), memory_mb=memory_mb)
    finally:
        shutil.rmtree(d, ignore_errors=True)


SUMMARY_RE = re.compile(r"(\d+) (passed|failed|errors?|xfailed|xpassed|skipped|deselected)")


def parse_pytest(out: str) -> tuple[int, int, int]:
    passed = failed = errors = 0
    lines = [ln for ln in out.strip().splitlines() if SUMMARY_RE.search(ln) and " in " in ln]
    for n, kind in SUMMARY_RE.findall(lines[-1] if lines else ""):
        n = int(n)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        elif kind.startswith("error"):
            errors = n
    return passed, failed, errors


def run_tests(files: dict[str, str], timeout_s: int = 120, live: bool = False, *,
              memory_mb: int = MEMORY_DEFAULT) -> TestResult:
    """live=False deselects @live tests (the Chef's coder iterations); live=True runs every test (the recorded run)."""
    d = _rundir(files)
    sel = [] if live else ["-m", "not live"]
    try:
        r = _exec(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "markers=live: live smoke test",
                   *sel, "--color=no", "test_tool.py"], d, timeout_s=max(1, min(timeout_s, 300)), memory_mb=memory_mb)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    passed, failed, errors = parse_pytest(r.stdout)
    if r.exit != 0 and not (failed or errors):
        errors = 1  # collection crash, timeout or interpreter error
    log = r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr.strip() else "")
    if r.timed_out:
        log += f"\n[timeout after {timeout_s}s]"
    return TestResult(passed, failed, errors, clip(log), r.duration_ms, r.timed_out)


def run_tool(manifest: dict, files: dict[str, str], args: dict, token: str, timeout_s: int, *,
             sock: str = RT_SOCK) -> dict:
    """Run tool.py's run(args, shed) as the tool user. Returns the result.json content."""
    _, memory_mb = limits(manifest)
    d = _rundir({k: v for k, v in files.items() if k == "tool.py"} | {"args.json": json.dumps(args)})
    try:
        r = _exec(["python3", "-m", "shed_sdk.main", str(d / "tool"), str(d / "tool" / "args.json"),
                   str(d / "out" / "result.json")], d, timeout_s=max(1, timeout_s), memory_mb=memory_mb,
                  token=token, sock=sock)
        res = d / "out" / "result.json"
        if r.timed_out:
            return {"ok": False, "error": f"timeout after {timeout_s}s", "stderr": clip(r.stderr, 2000)}
        if not res.exists():
            return {"ok": False, "error": f"the tool wrote no result (exit {r.exit})", "stderr": clip(r.stderr, 2000)}
        try:
            out = json.loads(res.read_text())
        except ValueError as e:
            return {"ok": False, "error": f"bad result.json: {e}"}
        if not out.get("ok") and r.stderr.strip():
            out.setdefault("stderr", clip(r.stderr, 2000))
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)
