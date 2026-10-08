# Taltempla v7: token-lean A/B + Big Chef (prototype tonight)

## Context
The v6 plan (ARCHITECTURE.md) spends tokens on every turn: a gap pass, triage, a stack pass, an MCP catalog that changes the tool list mid-session and breaks DeepSeek's prompt cache, and a full catalog in the context. New goals from the user:
- minimum tokens, peak UX, savings through REUSE;
- quick to decide to BUILD a comprehensive tool and use it in the SAME session;
- two grades of tools (big ones composed of small ones);
- agentic, self-contained tools in the Toolshed that carry their own skills and can chain other tools;
- a carefully built tool-building agent, the **Big Chef**;
- the spend monitor is integral;
- a working prototype tonight.

**Decision (revised): Module A is a Python CLI from scratch, not Pi.** After the redesign, Pi's main features are switched off or replaced:
- MCP is dropped.
- The built-in tools are excluded.
- The system prompt is replaced.
- The spend governor moves to the meter.

What would remain is the TUI polish. Pi would cost a second language, a spike, undocumented hooks and proxy-provider configuration. One language (Python 3.13) everywhere wins tonight. The loop is about 200 lines: an OpenAI-compatible client with `reasoning_content` passthrough and tool calls. The UI is `rich` (markdown, live status) plus `prompt_toolkit` (input, history, select dialogs).

Naming: **Module A** = the CLI (Python). **Module B** = the Toolshed (rootless Podman).

User decisions:
- A starts with lookup plus Big Chef.
- Module A is a Python CLI from scratch (Pi dropped, see above).
- Open net, no third-party keys.
- A security pass runs before the user sees a tool.
- The user approves every install.
- The user approves every tool use unless they chose "always allow".
- A big tool's approval lists its sub-tools. The user is never asked about sub-tools.

Repo facts:
- The key is in `.env` as `OAI_COMPATIBLE_KEY`.
- Pi is installed at `/usr/local/bin/pi`.
- This box is the dev machine (100.96.0.1). Host apt: `podman passt uidmap`; uv comes from its official installer.
- `toolshed/packages.json` has 908 packages in 21 categories, without per-package descriptions.

## Preflight findings (spike, 2026-10-08)
- **Local files:** the operator copies `.env` and `toolshed/packages.json` into this directory. `.env` is in `.gitignore`. If a file is missing, ask the operator. Do not read `../Taltempla/` or another sibling directory.
- **Host is ready:** uv 0.12.24 (`~/.local/bin`), rootless podman 5.4.2 (netavark, pasta), subuid `ak:100000:65536`, setpriv, prlimit.
- **Images pull and are cached:** `docker.io/library/python:3.13-slim`, `ghcr.io/astral-sh/uv:0.12.24`. Pin the uv tag to 0.12.24, the same as the host.
- **Inside the container:** `setpriv --reuid/--regid --clear-groups --no-new-privs` works. `prlimit --as --nproc` works. PyPI is reachable (open net).
- **⭐ Container → meter, answered:**
  - `host.containers.internal` is 169.254.1.2. It reaches a host listener only on `0.0.0.0`, not on `127.0.0.1`. A `0.0.0.0` bind puts the meter on the LAN.
  - A unix socket in a bind-mounted directory works. With the directory at 0700 and the socket at 0600, container root (shedd) connects and the `tool` user gets EACCES.
  - **Proposal:** the meter listens on `.frank/run/meter.sock`, which is mounted at `/run/meter`. This replaces `:7701`. Tools never reach the meter; `shed.llm` goes through shedd.
- **⭐ setpriv in rootless podman, answered:** it works (see above). Record both answers as decisions with D32+.
- **DeepSeek (verified):**
  - The balance is USD 6.06, which is below the $20 total cap. The balance is the real provider stop.
  - `/models` lists `deepseek-flash` (V4.1-Flash) and `deepseek-v4-pro`. The effort levels are `low|high|max`.
  - A tool call with thinking on (the default) and `reasoning_effort:"low"` returns `content:""`, `reasoning_content`, `tool_calls[{id, type, function{name, arguments: JSON string}}]` and `finish_reason:"tool_calls"`.
  - `usage` has `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `completion_tokens` and `completion_tokens_details.reasoning_tokens`.
- **Git:** `origin` is `github.com:matej-stepan/agents007_frankenstein_pentdragon`.

## Architecture
```
HOST: taltempla CLI (Python, one process): agent loop + UI + meter thread
      meter = the ONLY holder of the key; :7701 OpenAI-compat for B (grant tokens); ledger.db
      the agent loop calls the meter in-process (same reserve/settle path)
CONTAINER B: shedd (Python stdlib): admin API :7700 (admin token), runtime socket for tools,
      Big Chef, runner (setpriv uid "tool", prlimit, timeout, env -i), /data/shed.db (+FTS5), /work = ./workspace
```
- **The meter** is the single point for spend.
  - Every LLM call goes through it: A's agent, the Chef and agentic tools.
  - It authenticates the call with a scoped grant (main / build / tool_run). It injects the key and clamps `max_tokens`.
  - It **reserves before** the call against the build/run/session/total caps ($0.60/$2/$5/$20) and refuses with a clean 402 if a cap fails. It **settles after** the call with real usage, at peak or off-peak prices.
  - This replaces the `ctx.abort` governor.
- **The main agent in A has a fixed toolset**: `lookup`, `use_tool(name,args)`, `big_chef(task,need,lookup_id)`, `ws_read` and `ws_write`.
  - There is no MCP. The tool schemas never change, so the prompt cache survives and a new tool is usable at once.
  - The system prompt is our own: about 200 tokens and byte-stable.
  - There is no shell and no host file access by construction: the CLI has only these five tools.
- **Lookup has no LLM.** It runs FTS5 bm25 over name, summary, keywords, description and skill, plus a coverage score. Its reply is at most 3 rows, about 120 tokens, with `fit=good|partial|none`. `lookup(tool=X)` returns one tool's SKILL.md and schema. Big tools rank first.
- **Tool manifest** (`contract/manifest.schema.json`):
  - fields: name, grade (big|small), summary ≤120 chars, description, keywords, input/output schema, `uses` (chained tools), deps, permissions (network, LLM USD cap, files), limits, examples;
  - files: `tool.py` (`run(args, shed)`), `test_tool.py`, `SKILL.md` ≤1200 chars;
  - two hashes: the content hash (binds tests and install approval) and the permission hash (binds "always allow").
- **Chaining and agentic tools inside B** go through the SDK and the runtime socket with a per-invocation run token:
  - `shed.call(name,args)` works only for names in `uses`, within the depth, subcall and deadline limits.
  - `shed.llm(...)` goes to the meter on the root grant, so all spend is charged to the parent run.
  - `shed.registry` is read-only.
  - Outputs above 6 KB go to `/work/out/`, and A gets a preview and the path.
  - Tests use `shed.mock(...)` and `shed.mock_llm(...)`, plus at most one live smoke test.

## The Big Chef (in B, streams NDJSON trace lines to A)
| Phase | Role | Context (fresh per role, stable prefix first) | Output |
|---|---|---|---|
| P1 Plan | flash high, ≤6 turns | rules, manifest schema, SDK cheat sheet, "comprehensive tool" checklist + task, need, lookup top-3; tools `explore` (top-5/page), `pkg_search` (top-8), `read_tool_skill`, `submit_plan` | big-tool spec + small tools as reuse@v or new spec (generic small tools only) |
| P2 Tests | flash low | ONE spec + the interfaces of its `uses`, never code | `test_tool.py`; a sanity check that tests fail on a stub |
| P3 Code | flash high, one append-only conversation per tool | spec, manifest, tests, uses-skills; `probe`, `request_package` | full file first, then SEARCH/REPLACE; gets back only failure excerpts (≤4 KB); one DISPUTE round goes to the test writer |
| P4 Security | static AST checks (ban subprocess/eval/env/paths; literal `shed.call` ⊆ uses) + flash reviewer | manifest, code, static report, test summary | VERDICT; a reject gives 1 more coder iteration |
| P5 Handoff | code | — | gate payload: tool tree (reused/new), permissions, deps, tests n/n + iterations, verdict, cost |

- **Order:** plan → each new small tool (P2–P4) → the big tool (P2–P4) → P5.
- **Caps per build:** $0.60, 3 new small tools, 4+1 iterations per tool, 40 LLM turns, 12 min.
- **"Comprehensive" checklist:** validation, ≥2 sources or a fallback, pagination, dedup, normalised units, `{results,warnings,sources}`, retries, ranking with reasons, a mocked offline core, and a SKILL.md that states the limits.
- **Repair mode:** a regression test from the failing input → version n+1 with `parent` set (this gives the rollback demo).

## Approvals and UX (A)
- **Install gate** (prompt_toolkit select): Install + run once / Install + always allow / Show code·tests·log / Reject. `register` needs a passed test run, a review and an approval for each content hash.
- **Use gate** (checked in the loop before each `use_tool` call): "Run X v1? chains a, b, c · net · LLM ≤$0.05" → Allow once / Always allow / Deny. "Always allow" goes to `.frank/permissions.json`, keyed by the permission hash. Sub-tools are never asked about. `/allow` lists and revokes entries.
- **Traces:** one dim line per Chef step, streamed from the NDJSON.
- **Status line:** `$sess · run $ · tok % cached · saved $ · cap left`.
- **Commands:** `/shed`, `/cost` (by run, role and tool, with saved-by-reuse), `/rollback`, `/allow`.

## Cut from v6
Pi (D1 → the Python CLI), gap pass (D6), triage (D30), stack pass (D5, merged into the plan phase), MCP agent channel with list_changed and fresh context (D23, D31), resolve_gap and the gap list, the Pro reviewer default (PR-8 → flash + static checks, Pro as a switch), and PR-4 (shedd becomes Python).

## Files
- **Code:**
  - `cli/taltempla/{main.py,loop.py,llm.py,meter.py,ledger.sql,prices.json,prompt.md,shed_client.py,gate.py,approvals.py,ws_tools.py,commands.py,ui.py}`
  - `toolshed/{Containerfile,tiers.py,server/shed/{app,db,schema.sql,lookup,runner,chain,llm,pkgindex}.py,server/shed/chef/{orchestrator.py,static_check.py,prompts/*.md},sdk/shed_sdk/{main,testing}.py}`
  - `pyproject.toml` + `uv.lock` (root), `contract/`, `Makefile`, `spikes/`, `tests/`
- **Python 3.13 everywhere.** B's server is stdlib only. A uses rich and prompt_toolkit. A and B share one LLM client file, which is copied into the image.

## Package management: uv for the whole project
- **uv is the only Python package manager**, on the host and in the image. Do not use pip, venv or apt Python packages.
- **One uv workspace at the repo root:**
  - `pyproject.toml` pins `requires-python = "==3.13.*"` and lists the members `cli` and `toolshed`.
  - One `uv.lock` is committed.
  - Dev dependencies (`pytest`, `ruff`) are in a `dev` group.
- **Host:** `uv sync` makes `.venv`. Every command runs as `uv run …`.
- **Image:**
  - The Containerfile copies the `uv` binary from `ghcr.io/astral-sh/uv` with a pinned tag.
  - It installs the server and SDK with `uv sync --frozen --no-dev`.
  - It installs tier A from `tiers.py` with `uv pip install --system`.
  - `request_package` uses `uv pip install`, for catalog names only.
- **Host apt packages are system packages only:** `podman`, `passt`, `uidmap`. uv comes from the official installer, user level.

## Makefile: the single entry point
The Makefile is integral. Every task goes through it, and the operator never needs another command.
- **Default target `make help`** prints every target with a one-line description, taken from `## ` comments.
- **Preflight:** `make doctor` checks:
  - uv, podman and rootless mode;
  - the subuid entry;
  - that `.env` has the key;
  - that the image exists;
  - that the toolshed answers `health`.
  
  Each failed check prints the fix. `make run` calls `doctor` first.
- **Idempotent targets:** `toolshed-up` builds the image if it is missing. `run` starts the toolshed if it is down.
- **Variables:** `MODEL`, `CAP_RUN`, `CAP_SESSION` and `GATE` can be overridden on the command line, e.g. `make run CAP_RUN=1`.

| Target | Function |
|---|---|
| `help` | List the targets (default) |
| `deps` | Print the apt line for the system packages; install uv if missing; `uv sync` |
| `doctor` | Run the preflight checks |
| `image` | Build the toolshed image (tiers + uv inside) |
| `toolshed-up` / `-down` / `-reset` / `-shell` / `-logs` | Start, stop (keep the data), delete all data, open a shell as the tool user, show the logs |
| `run` | Start the CLI (`uv run taltempla`) |
| `shed` | Print the registry (before the run, for the demo) |
| `cost` | Print the ledger by run, role and tool |
| `rollback T= V=` | Roll back one tool |
| `history T=` | Print the version and event history of a tool |
| `test` | `uv run pytest` (unit + contract tests) |
| `lint` / `fmt` | `uv run ruff check` / `ruff format` |
| `lock` | `uv lock` |
| `spike-shed` | The container networking and setpriv spike |
| `demo-reset` | Back up and clear the DB and ledger for a clean recording |
| `clean` | Remove `.venv`, caches and build output (keeps the data) |
- **Docs:**
  - ARCHITECTURE.md gets D32–D44 (the Python CLI replaces D1/Pi; fixed toolset/no MCP; two grades; FTS lookup + lookup_id gap rule; the Chef in B; the meter + grants; chaining scope; approvals; Python everywhere; flash reviewer; large outputs to files; package tiers + pkg_search; open net). The old rows become "Replaced by Dxx". Rewrite §2–§11, §15 and §16 in STE.
  - OPEN_QUESTIONS: remove the answered questions. Add ⭐ container→meter networking, ⭐ setpriv in rootless podman, and the lookup thresholds.

## Tonight's order
| Time | Work | Exit |
|---|---|---|
| 0:00 | Containerfile + tiers.py; start `make image` in the background | — |
| 0:10–1:00 | **CLI core:** the DeepSeek client (reasoning_content round trip, tool calls) + the meter (grants, reserve/settle, ledger, :7701). **Shed spike in parallel:** setpriv, container→host :7701, the /work mount | A chat with a cost line works |
| 1:00–2:00 | shedd core (DB, FTS, lookup, runner, runtime socket, invoke, register, rollback) | A chained fixture invoke, metered |
| 2:00–2:40 | CLI tools, gates, status line, commands | M1: lookup → use_tool → chain → cost shown |
| 2:40–4:40 | Big Chef P1–P5 + install gate | M2: fetch_page built; M3: the real-estate bundle is built and used in one session |
| 4:40–5:40 | Prompt tuning, reuse/saved accounting, repair, read-only registry | The second build reuses small tools |
| 5:40–6:50 | Dry runs, fixes, lookup threshold tuning | Script passes twice |
| 6:50–8:00 | Docs D32+, demo-reset, video | — |

## Verification
- **`make test`:**
  - the meter refuses over cap, parses usage and returns 401 on a bad grant;
  - register refuses without a test run, a review and an approval;
  - a call outside `uses` is refused;
  - the depth and subcall limits work;
  - the tool user cannot read the DB or the token;
  - the append-only triggers work;
  - the static check catches subprocess and a dynamic `shed.call`.
- **Demo:**
  1. `make toolshed-reset && make shed` shows an empty registry.
  2. Session 1: "cheapest house in Brno-venkov under 8M CZK" → fit=none → Chef trace (tests fail→pass, security ok) → gate → use → answer with URLs → `/cost`.
  3. Session 2 (a new `make run`): "5 cheapest used Octavia combi in Prague" → fit=partial → composed from the session-1 small tools, no rebuild, "saved $X".
  4. Session 3: "which tools cost most / fail most" → the Chef builds `toolshed_insights` (read-only registry).
  5. Repair → v2, then `/rollback`.
  6. `/cost`, compared with the DeepSeek `/user/balance` change.
