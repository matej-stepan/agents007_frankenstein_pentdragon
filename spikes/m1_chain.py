"""M1: the metered chain (make m1). Needs the toolshed up and the CLI not running.

The host meter serves .frank/run/meter.sock. The in-container selftest (temp DB, fixture tools only) invokes a
big fixture that chains st_double and st_llm; st_llm calls shed.llm with a tool_run grant made here. The key
stays in this process; the container only gets the grant. Prints the ledger rows of this meter session.
"""

import subprocess
import sys
from pathlib import Path

from taltempla.meter import from_env

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    m = from_env(ROOT)
    m.serve(ROOT / ".frank" / "run" / "meter.sock")
    run = m.grant("run", parent=None, cap_usd=0.05, label="m1 selftest")
    g = m.grant("tool_run", parent=run, cap_usd=0.01, label="st_llmcombo", tool="st_llmcombo")
    try:
        rc = subprocess.run(["podman", "exec", "-e", f"SELFTEST_GRANT={g}", "taltempla-shed",
                             "python3", "-m", "shed.selftest"], check=False).returncode
        rows = m._db.execute("SELECT role, tool, grant_kind, hit, miss, out, cost_usd, status FROM calls "
                             "WHERE session_id = ?", (m.session_id,)).fetchall()
        print(f"ledger rows for meter session {m.session_id}:")
        for r in rows:
            print("  role={} tool={} grant={} hit={} miss={} out={} cost=${:.6f} {}".format(*r))
        ok = rc == 0 and any(r[0] == "tool" and r[1] == "st_llm" and r[7] == "ok" and r[6] > 0 for r in rows)
        print("M1 metered chain:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        m.revoke(run)
        m.close()


if __name__ == "__main__":
    sys.exit(main())
