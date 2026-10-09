# Taltempla architecture

Status: v7, 2026-10-08. Language: ASD-STE100. Plan: [PLAN.md](PLAN.md). Interfaces (binding): [contract/INTERFACES.md](contract/INTERFACES.md) and [contract/manifest.schema.json](contract/manifest.schema.json).
Brief: [SUBJECT.md](SUBJECT.md). Open questions: [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md). Raw research: [research/](research/) (v6, not a decision).
**PROPOSAL** = not a decision yet (§13). **LATER** = not in the prototype. "Contract §n" = a section of `contract/INTERFACES.md`.

## 1. Modules
**Module A, the CLI**, is a Python 3.13 program on the host (D32). It has the agent loop, the operator interface and the meter. The meter is the only holder of the DeepSeek key. The CLI never executes generated code.
**Module B, the toolshed**, is one long-lived rootless Podman container. Its server, shedd, keeps the tool database, does the lookup, runs the Big Chef and executes all generated code as the tool user. It has no credentials. Its LLM calls go through the meter.

## 2. Terms
| Term | Definition |
|---|---|
| Admin API | The HTTP API of shedd for CLI code (contract §5). It needs the admin token. The model cannot reach it. |
| Admin token | A random local secret in `.frank/admin.token`. Only the CLI and shedd know it. It is not a credential. |
| Big Chef | The agent in the toolshed that builds tools for a gap, in the phases P1–P5 (§9). |
| Big tool | A task-level tool that calls small tools. Its `uses` is not empty. |
| Build | One Big Chef sequence for one gap or one repair. |
| Build log | The records of a build in the tool database: the trace, each test run, each review and the result. |
| Cap | A limit that code enforces on USD, tools, iterations, turns, time, memory or processes (§10). |
| Content hash | The hash of the manifest and the files of a draft (contract §6). It binds the test runs, the review and the install approval. |
| Credential | A key for an external service, for example the DeepSeek key. |
| Draft | The files of a tool that the toolshed did not register. |
| Exchange directory | The host directory `./workspace`, with `in/` and `out/`. The container mounts it at `/work`. Files go between the modules only through it. |
| Fit | The lookup result: `good`, `partial` or `none`. |
| Gap | A capability that a task needs and that the toolset does not have: a lookup with fit `none` or `partial`, or a failed invocation. |
| Grant | A random token from the meter for one scope: `session`, `run`, `build` or `tool_run`. Each LLM call needs a grant. |
| Install gate | The CLI step where the operator approves or rejects the new tools of a build. |
| Ledger | The meter record of each LLM call, its USD cost and the USD that reuse saved (`.frank/ledger.db`). |
| Lookup | A search of the tool database without an LLM. It returns 3 rows or fewer, a fit and a lookup ID. |
| Main agent | The agent loop in the CLI that answers the operator. |
| Manifest | The declaration of a tool: grade, summary, interfaces, `uses`, deps, permissions, limits and examples. |
| Meter | The CLI component that holds the key, checks grants and caps, sends each LLM call and writes the ledger. |
| Operator | The person at the terminal. |
| Permission hash | The hash of the name, `uses`, deps and permissions of a tool. It binds "always allow". |
| Repair | A build for a failed invocation. It makes version n+1 with a parent. |
| Root invocation | One `use_tool` call and all tool calls that it starts. |
| Run | One operator prompt, until the main agent stops. |
| Run token | A random token for one tool process. It authenticates the process on the runtime socket. |
| Runner | The part of shedd that executes agent code as the tool user, with limits. |
| Runtime socket | The unix socket of shedd in the container (`/run/shed/rt.sock`). Tool code uses it for `call`, `llm` and `registry`. |
| SDK | `shed_sdk`: the library for tool code and tests (contract §6). |
| Session | One CLI process, from `make run` to exit. A session contains one or more runs. |
| shedd | The toolshed server: the trusted process in the container. |
| Skill | The `SKILL.md` of a tool (1200 characters or fewer): how and when to use the tool, and its limits. |
| Small tool | A generic tool that other tools can use again. |
| Test set | The `test_tool.py` of a draft. Only the P2 test writer writes it. |
| Tier | A group of packages. Tier A is in the image. The other catalog packages install on request. |
| Tool | A capability: a manifest, `tool.py`, a test set and a skill. |
| Tool database | The SQLite file `/data/shed.db` in the toolshed. The brief calls it the registry. |
| Tool user | The low-privilege user `tool` (uid 1000) in the toolshed. It executes all agent code. |
| Toolset | The active version of each tool. |
| Use gate | The CLI step where the operator approves one tool call. |
| Version | One registered state of a tool. A content hash identifies it. It can have a parent version. |
| Workspace tools | `ws_read` and `ws_write`: main agent tools for text files in the exchange directory. |

## 3. Rules
1. Generated code executes only in the toolshed, as the tool user. The host never executes, imports or `exec`s it.
2. The CLI has no host code execution: no shell, no `exec`, no writes outside the exchange directory.
3. Only the meter holds the DeepSeek key. Each LLM call goes through the meter with a grant. The toolshed has no credentials.
4. All LLM calls go to the DeepSeek API.
5. The toolshed registers a draft only after a passed test run, an `approve` review and an operator approval, all for the same content hash. The build log shows each test run.
6. The main agent has five fixed tools (§6). Only CLI code uses the admin API.
7. Each tool declares its permissions. The install gate and the use gate show them.
8. Code enforces caps on USD, tools, iterations, turns, time, memory and processes (§10).
9. Each version comes from a Big Chef build and the install gate. No person and no script puts a tool into the tool database (D47).
10. The tool database keeps all versions and records. History only goes forward. The operator can roll back a tool.
11. Each gap comes from a task. `big_chef` accepts only a lookup ID from the current session with fit `none` or `partial`, or a failed invocation (D35).
12. Capabilities can grow, authority cannot. A tool can read the registry statistics, but it cannot change them. A tool can call only the tools in its `uses`. Only the operator changes the toolset: at the install gate or with `/rollback`.
13. No key enters an LLM context, a log, a trace, the tool database, the toolshed or a tool.

## 4. Module boundary
| Responsibility | Owner |
|---|---|
| Operator interface: prompts, gates, commands, status line | CLI |
| The meter: the DeepSeek key, grants, USD caps, the ledger | CLI |
| Approvals: install, use, "always allow" (`.frank/permissions.json`) | CLI |
| Lookup, tool database, versions, rollback | Toolshed |
| Big Chef (plan, tests, code, security, handoff) and the build log | Toolshed |
| Code execution, chaining, packages and the limits of each execution | Toolshed |

| Channel | Direction | Transport | Caller | Contract |
|---|---|---|---|---|
| Admin API | CLI → toolshed | HTTP on `127.0.0.1:7700`, admin token | CLI code only | §5 |
| Chef trace | Toolshed → CLI | NDJSON stream in the `/chef/build` reply | shedd | §8 |
| Meter | Toolshed → CLI | HTTP on the unix socket `.frank/run/meter.sock` (`/run/meter` in the container), grant | shedd only | §4 |
| Runtime socket | Tool code → shedd | JSON lines on `/run/shed/rt.sock`, run token | Tool code | §7 |
| Files | Both | Exchange directory | shedd, workspace tools | §2 |

**Never crosses:** the key, the host environment and requests for host code execution.

## 5. Boundary enforcement
**E** = code or configuration enforces the rule. **CONV** = a convention only, in the prototype.

| # | Rule | Mechanism | Status |
|---|---|---|---|
| E1 | 1 | The runner executes all agent code as the tool user: `setpriv` (uid 1000, no new privileges, no groups), `prlimit`, `timeout`, `env -i` and a new directory for each run (D46). The container is rootless. | E |
| E2 | 2, 6 | The CLI is our code. The main agent gets five tool schemas and no other tool. There is no shell tool. | E |
| E3 | 2 | The workspace tools refuse each path outside `workspace/`. | E |
| E4 | 3, 13 | `make toolshed-up` gives the container no `.env`, no host environment, no home directory and no Podman socket. It mounts only the data volume, the exchange directory and the meter socket directory. | E |
| E5 | 3 | Only the meter reads the key. The meter refuses a bad grant (401). The socket directory is 0700 and the socket is 0600: shedd (container root) connects, the tool user gets EACCES (D45). | E |
| E6 | 5, 6 | `register` needs the admin token, a passed test run, an `approve` review and an approval for each content hash (else 409). shedd removes the admin token from its environment at start. `/data` is 0700 root. | E |
| E7 | 5 | Only P2 writes the test set. P3 has no operation that changes it. P3 can start one DISPUTE round with the test writer (D20). | E |
| E8 | 7 | `llm_usd`: the runtime socket refuses `llm` when it is 0, and the meter caps the root grant. `network` and `files`: the gates show them, the runner does not apply them. | E / CONV |
| E9 | 8 | The meter reserves before each call and settles after it (D37). The Big Chef counts tools, iterations, turns and time. The runner applies time, memory and process limits. | E |
| E10 | 9, 10 | Triggers make versions, test runs, reviews, approvals, events and build logs append-only. The admin API has no delete operation. | E |
| E11 | 11 | `/chef/build` refuses (400) a lookup ID that is not valid, and a repair without a failed invocation. | E |
| E12 | 12 | The runtime socket refuses a `call` outside `uses` or over the depth and subcall limits. `registry` returns only statistics. The P4 static check refuses a `shed.call` name that is not a literal. | E |
| E13 | 13 | The ledger, the traces and the tool database record no request headers. | E |

## 6. CLI module (Module A)
One Python process: `uv run taltempla` (files: contract §1). It reads `OAI_COMPATIBLE_KEY` from `.env` and gives it only to the meter.

| Component | Files | Function |
|---|---|---|
| Agent loop | `loop.py`, `prompt.md` | Chat with tool calls. It keeps `reasoning_content` in each assistant message. Our system prompt: about 200 tokens, byte-stable. |
| LLM client | `llm.py` | OpenAI-compatible, standard library only (contract §3). The image has a copy for shedd. |
| Meter | `meter.py`, `ledger.sql`, `prices.json` | A thread in the CLI process: grants, reserve and settle, the ledger, the unix socket server for shedd (contract §4). The agent loop calls it in-process, on the same path. |
| Toolshed client | `shed_client.py` | Admin API calls. It reads the NDJSON stream of the Big Chef. |
| Install gate | `gate.py` | A `prompt_toolkit` choice: Install + run once / Install + always allow / Show code·tests·log / Reject. |
| Use gate | `approvals.py` | Before each `use_tool`: "Run X v1? chains a, b, c · net · LLM ≤$0.05" → Allow once / Always allow / Deny. It keeps `.frank/permissions.json`. |
| Workspace tools | `ws_tools.py` | `ws_read` (64 KB or less) and `ws_write`, jailed to `workspace/`. |
| Commands | `commands.py` | `/shed`, `/cost` (by run, role and tool, with the savings from reuse), `/rollback`, `/allow`. |
| UI | `ui.py`, `main.py` | `rich` markdown. One dim line for each Big Chef step. Status line: `$sess · run $ · tok % cached · saved $ · cap left`. |

The main agent tools are fixed (D33, contract §9): `lookup`, `use_tool`, `big_chef`, `ws_read`, `ws_write`.

## 7. Inference
All LLM calls go to the DeepSeek API through the meter. Models: `deepseek-flash` (V4.1-Flash) and `deepseek-v4-pro`. Effort levels: `low`, `high`, `max`.

| Role | Model | Effort | Context | Output | `max_tokens` |
|---|---|---|---|---|---|
| Main agent | `deepseek-flash` (`MODEL`) | — | System prompt, session history | Text and tool calls | 8192 |
| P1 Plan | `deepseek-flash` | `high` | Rules, manifest schema, SDK sheet, checklist, task, need, lookup top 3 | Spec tree (`submit_plan`) | 16384 |
| P2 Tests | `deepseek-flash` | `low` | One spec and the interfaces of its `uses`, never code | `test_tool.py` | 12288 |
| P3 Code | `deepseek-flash` | `high` | Spec, manifest, tests, skills of `uses`, failure excerpts | Tool code | 16384 |
| P4 Security | `deepseek-flash`; `deepseek-v4-pro` as a switch | — | Manifest, code, static report, test summary | VERDICT | 6144 |
| Agentic tool | `deepseek-flash` | — | The `shed.llm` prompt of the tool | Text or JSON | 4096 |

1. Thinking is on by default. The client never sends a forced `tool_choice`.
2. DeepSeek returns HTTP 400 when a request does not include the earlier `reasoning_content`. Thus the client keeps it in each assistant message.
3. Cost of one call = cache-hit tokens × hit price + cache-miss tokens × miss price + output tokens × output price. The meter uses the peak or off-peak price by the UTC start time (contract §4).
4. The ledger is the authority for cost. The provider balance is the real stop (USD 6.06 at the preflight).
5. The system prompt and the tool schemas do not change in a session. Thus the prompt cache stays valid. Each Big Chef role puts its stable prefix first.

## 8. Toolshed module (Module B)
One long-lived rootless Podman container from `python:3.13-slim` (constants: contract §2). `make toolshed-up` starts it. `make toolshed-down` stops it and keeps the data volume.

| Part | Files | Function |
|---|---|---|
| shedd | `app.py` | The trusted process, as container root (D46). Admin API on `:7700`, runtime socket, Big Chef, runner. Standard library only (D40). |
| Tool database | `db.py`, `schema.sql` | SQLite with WAL and FTS5 in `/data` (0700 root). Append-only: versions, test runs, reviews, approvals, events, build logs. Mutable: active pointers, drafts, build status, lookups, packages. |
| Lookup | `lookup.py` | FTS5 bm25 over name, summary, keywords, description and skill, plus a coverage score. Big tools first. It records each lookup (D35). |
| Runner | `runner.py` | `probe`, `run_tests` and `run_tool` as the tool user (D46). |
| Chaining | `chain.py` | The runtime socket: run tokens, `uses` scope, depth, subcalls, deadline (D38). |
| Meter client | `llm.py` | Calls the meter on `/run/meter/meter.sock`. A 402 raises `CapExceeded`. |
| Packages | `pkgindex.py`, `tiers.py` | The catalog `packages.json`. Tier A is in the image. `request_package` installs catalog names only (D43). |
| Big Chef | `chef/` | `orchestrator.py`, `static_check.py`, `prompts/` (§9). |
| SDK | `shed_sdk` | `Shed` (`call`, `llm`, `registry`, `log`, `out_dir`) for tool code. `MockShed` for tests. |

Admin API routes (contract §5): `health`, `tools`, `lookup`, `invoke`, `chef/build`, `drafts`, `register`, `reject`, `rollback`, `history`, `stats`.
Tool package (contract §6): `manifest.json`, `tool.py` with `run(args, shed)`, `test_tool.py`, `SKILL.md`. A big tool returns `{results, warnings, sources}`. Tests use `MockShed` and one live smoke test or fewer.

## 9. Sequences
Steps: **S** = start, **1–6** = run, **P1–P5** = Big Chef build, **R** = repair.

| Step | Module | Action |
|---|---|---|
| S1 | Toolshed | `make toolshed-up` builds the image and makes the admin token if necessary. Then it starts the container. |
| S2 | CLI | `make run` does `make doctor` and starts the toolshed if it is down. The CLI reads `.env`, starts the meter socket, calls `health` and opens a session grant. The ledger records the start balance. |
| 1 | CLI | The operator sends a prompt. The meter opens a run grant. |
| 2 | CLI → Toolshed | The main agent calls `lookup`. shedd returns 3 rows or fewer, a fit and a lookup ID. |
| 3 | CLI → Toolshed | Fit `good`: the main agent reads the skill if necessary (`lookup(tool=X)`) and calls `use_tool`. The use gate asks the operator. shedd executes the tool. Sub-tool calls go through the runtime socket. `shed.llm` goes through shedd to the meter on the root grant. |
| 4 | CLI → Toolshed | Fit `none` or `partial`: the main agent calls `big_chef` with the lookup ID. The meter opens a build grant. shedd checks the gap rule and does P1–P5. The CLI shows one trace line for each step, then the install gate. |
| 5 | CLI → Toolshed | Approval: the CLI calls `register` for each content hash. The main agent then calls `use_tool` with the new tool, in the same run. Reject: the CLI calls `reject`. |
| 6 | CLI | The run stops. The status line shows the run cost. The ledger records the USD that reuse saved. |
| P1 | Toolshed | Plan, 6 turns or fewer. Tools: `explore` (top 5 for each page), `pkg_search` (top 8), `read_tool_skill`, `submit_plan`. Output: the big tool spec and, for each small tool, `reuse@v` or a new spec. New small tools must be generic. The plan sets deps and permissions. |
| P2 | Toolshed | The test writer writes `test_tool.py` for one spec. A sanity check makes sure that the tests fail on a stub. |
| P3 | Toolshed | The coder writes the full file, then SEARCH/REPLACE edits, in one append-only conversation for each tool. It gets back only failure excerpts (4 KB or less). Tools: `probe`, `request_package`. One DISPUTE round goes to the test writer. |
| P4 | Toolshed | Static AST checks, then the flash reviewer (D41). VERDICT `approve` or `reject`. A reject gives one more coder iteration. |
| P5 | Toolshed → CLI | Handoff: the tool tree (reused and new), permissions, deps, tests n/n, iterations, verdict, cost. |
| R1 | CLI → Toolshed | After a failed invocation, the main agent calls `big_chef` with `repair_of`. The test writer adds a regression test from the failed input. The build makes version n+1 with a parent. `/rollback` moves back. |

Order: P1, then P2–P4 for each new small tool, then P2–P4 for the big tool, then P5.
"Comprehensive" checklist for a big tool: input validation, 2 sources or a fallback, pagination, deduplication, normalized units, `{results, warnings, sources}`, retries, ranking with reasons, a mocked offline core, and a skill that states the limits.

## 10. Caps and cost
| Cap | Default | Scope | Enforcer |
|---|---|---|---|
| USD | 0.60 / 2.00 / 5.00 / 20.00 | Build / run / session / total | Meter (grant chain) |
| Tool LLM USD | `permissions.llm_usd` (0.25 or less) | Each root invocation | Runtime socket, meter |
| `max_tokens` | 4096–16384, by role (§7) | Each LLM call | Meter |
| New small tools | 3 (+ the big tool) | Each build | Big Chef |
| Coder iterations | 4, + 1 after a reject | Each tool | Big Chef |
| LLM turns, wall time | 40, 12 min | Each build | Big Chef |
| Chain depth, subcalls | 3, 20, the root deadline | Each root invocation | Runtime socket |
| Time, memory, processes, files | 60 s (max 180), 2 GiB (max 4 GiB), 512, 1024 | Each execution | Runner |
| Processes, memory | 2048, 4 GB | The container | Podman |

- The meter reserves an estimate before each call. If a cap at a level fails, the meter refuses with a 402 and records a `refused` row. A call can cost more than its estimate (D37).
- `make run CAP_RUN=… CAP_SESSION=…` changes the caps for one session.
- The ledger keeps the total for all sessions. `/cost` shows it by run, role and tool, with the savings from reuse. It also compares the ledger with the change of the provider balance.

## 11. Project structure and Makefile
In git: `Makefile`, `pyproject.toml`, `uv.lock`, `cli/`, `toolshed/`, `contract/`, `tests/`, `spikes/`. Layout: contract §1.
Not in git: `.env`, `workspace/` (the exchange directory) and `.frank/` (ledger, permissions, admin token, history, meter socket).
The Makefile is the single entry point (D10). `make help` is the default target. Variables: `MODEL`, `CAP_RUN`, `CAP_SESSION`, `GATE`.

| Target | Function |
|---|---|
| `help` | List the targets (default). |
| `deps` | Print the apt line (`podman passt uidmap`). Install uv if necessary. `uv sync`. |
| `doctor` | Preflight checks: uv, rootless Podman, subuid, the key in `.env`, the image, `health`. Each failed check prints the fix. |
| `image` | Build the toolshed image. |
| `toolshed-up`, `-down`, `-reset`, `-shell`, `-logs` | Start. Stop and keep the data. Delete all data. Open a shell as the tool user. Show the logs. |
| `run` | Start the CLI. It does `doctor` first and starts the toolshed if it is down. |
| `shed`, `cost`, `history T=` | Print the registry, the ledger, or the history of one tool. |
| `rollback T= V=` | Roll back one tool. |
| `test`, `lint`, `fmt`, `lock` | pytest, `ruff check`, `ruff format`, `uv lock`. |
| `spike-shed`, `demo-reset`, `clean` | The container spike. Back up and clear the tool database and the ledger. Remove caches and build output. |

## 12. Stack
| Layer | Choice | Decision |
|---|---|---|
| CLI | Python 3.13, `rich`, `prompt_toolkit` 3.0.53, our OpenAI-compatible client | D32 |
| Inference | DeepSeek API: `deepseek-flash`; `deepseek-v4-pro` as a switch for P4 | D14, D41 |
| Toolshed | One rootless Podman container (`python:3.13-slim`), shedd on the standard library, SQLite with FTS5 | D28, D29, D40 |
| Packages | uv 0.12.24 on the host and in the image, package tiers | D40, D43 |

## 13. Proposals
| # | PROPOSAL | Rationale |
|---|---|---|
| PR-4 | Replaced by D40. | — |
| PR-5 | Closed by D32 (no Pi). | — |
| PR-6 | Closed by D32 (no Pi). | — |
| PR-7 | Closed by D32 (no Pi). | — |
| PR-8 | Replaced by D41. | — |
| PR-10 | A dedicated DeepSeek account with a small prepaid balance. | DeepSeek has no spend limit. An empty balance is the only provider stop. |
| PR-11 | Closed. The demo (§15) uses public listings, not personal data. | — |
| PR-14 | LATER: key injection. A proxy adds a key to the outbound request of a tool. The tool code never sees the key. | The mentor prefers injection (D27). D44 keeps third-party keys out of the prototype. |
| PR-15 | Adopted in D37: the ledger records the savings from reuse. `/cost` and the status line show them. | — |
| PR-16 | Twist: Frankenstein roles. The generator is "the Doctor", the test writer is "Igor", the reviewer is "the Mob". | Cheap: prompts and theme only. Easy to remember in the video. |

## 14. Decision log
| # | Decision | Reason |
|---|---|---|
| D1 | Replaced by D32. | — |
| D2 | Replaced by D13. | — |
| D3 | Generated code never executes on the host. | Brief hard rule 1. Also, the gaps stay real. |
| D4 | Replaced by D47. | — |
| D5 | Replaced by D36. | — |
| D6 | Replaced by D35. | — |
| D7 | Replaced by D37. | — |
| D8 | The toolshed is local. A VPS is an option for later. | Speed and a simple development loop. |
| D9 | Code execution and the tool database are one module, the toolshed. D28 sets the placement. | Simple design. |
| D10 | A Makefile dispatches all tasks. | One entry point. |
| D11 | Minimum safety: a rootless container, no credentials, timeouts. D27 gives the mentor answer. | Little time. The harness is more important. |
| D12 | The design documents use ASD-STE100. | Clear and short text. |
| D13 | Two modules, the CLI and the toolshed. Each responsibility has one owner (§4). | Code can enforce clear boundaries. |
| D14 | Inference uses only the DeepSeek API. | One provider, one price table, one ledger. |
| D17 | The CLI has no host code execution. | Brief hard rule 1. |
| D18 | A separate reviewer examines each draft. The operator approves at a gate. Code caps USD and iterations. | The brief encourages a gate. Brief hard rule 4. |
| D19 | Replaced by D28. | — |
| D20 | The test writer is a separate role with a fresh context. Only it can change the test set. | The tests do not see the code. The generator cannot weaken them. |
| D21 | Tools are Python only for the hackathon. | One runtime, one lock tool. |
| D22 | The team writes the tool database, its operations, the catalog and the operator commands. The agent builds or extends one management tool or more (§15). No import, export or check tools in the prototype. | Brief definition of done 3. |
| D23 | Replaced by D33. | — |
| D24 | Replaced by D37. | — |
| D25 | Development and the demo use the dev machine. `make deps` installs rootless Podman. | No daemon, no socket, a built-in timeout. |
| D26 | The first version is a minimal prototype that meets the brief (§16). | Little time. We extend the prototype after the hackathon. |
| D27 | Mentor answer (rwngwn, 2026-10-08): a remote sandbox is not necessary. The agent must not see keys directly. Ideally, the system injects keys. | It answers brief hard rule 1. |
| D28 | The toolshed is one long-lived rootless Podman container. The toolshed server is in it. Generated code executes as the tool user. | Dead simple. Replaces D19 (a new container for each call). |
| D29 | The tool database is SQLite only. Rollback moves an active pointer. | Dead simple. Replaces PR-3 (git and SQLite). |
| D30 | Replaced by D35. | — |
| D31 | Replaced by D33. | — |
| D32 | The CLI (Module A) is a Python 3.13 program that we write: an agent loop of about 200 lines, an OpenAI-compatible client that keeps `reasoning_content`, and a UI with `rich` and `prompt_toolkit`. Replaces D1. | v7 removes or replaces the main Pi features: MCP, the built-in tools, the system prompt and the governor. Pi adds a second language, a spike, undocumented hooks and proxy configuration. One language is faster. |
| D33 | The main agent has a fixed toolset: `lookup`, `use_tool`, `big_chef`, `ws_read`, `ws_write`. There is no MCP and no agent channel. The tool schemas and our system prompt (about 200 tokens) do not change in a session. The admin API stays HTTP on 127.0.0.1 with the admin token. Replaces D23 and D31. | The prompt cache stays valid. A new tool is available at once through `use_tool`, without a fresh context. No shell and no host file access, by construction. |
| D34 | Tools have two grades. A small tool is generic. A big tool is task-level and calls small tools (`uses` is not empty). Lookup shows big tools first (replaced by D53). | Reuse saves tokens and USD. A big tool gives a complete result in one call. |
| D35 | Lookup uses no LLM: FTS5 bm25 over name, summary, keywords, description and skill, plus a coverage score. The reply has 3 rows or fewer (about 120 tokens), a fit (`good`, `partial`, `none`) and a lookup ID. `lookup(tool=X)` returns the skill and the schemas of one tool (for a small tool: replaced by D53). Gap rule: `big_chef` needs a lookup ID from the current session with fit `none` or `partial`, or a failed invocation of the tool to repair. Replaces D6 and D30. | No LLM tokens for each reply. Code makes sure that each gap comes from a task (brief hard rule 3). |
| D36 | The Big Chef builds tools in the toolshed, in five phases: P1 plan, P2 tests, P3 code, P4 security, P5 handoff (§9). Each role gets a fresh context with the stable prefix first. P1 also sets the deps and the permissions. D20 stays. shedd streams one trace line for each step to the CLI. Replaces the forge of v6 and D5. | The builder is near the runner, the tool database and the packages. Short loops use fewer tokens. The CLI only shows the trace and the gate. |
| D37 | The meter is the single point for spend and the only holder of the key. It is a thread in the CLI process. Each LLM call (main agent, Big Chef, agentic tools) needs a grant: `session`, `run`, `build` or `tool_run`. The meter reserves an estimate against each cap in the grant chain, clamps `max_tokens`, adds the key, and settles from `usage` after the call. A failed cap gives a clean 402. Caps: USD 0.60 for each build, 2.00 for each run, 5.00 for each session, 20.00 in total (replaced by D54). The ledger is the authority for cost. It also records the savings from reuse. Replaces D7 and D24. | One enforcement point for all callers. The reserve stops most overshoots. A call can cost more than its estimate. We accept this. |
| D38 | Chaining scope. Tool code calls other tools only with `shed.call` through the runtime socket. Each process has its own run token. shedd refuses a name that is not in `uses`. Limits for each root invocation: depth 3, 20 subcalls, the root deadline. `shed.llm` goes through shedd to the meter on the root grant. Thus the root run pays for all spend. `shed.registry()` returns the `/stats` rows, read-only. | Composition without more authority. The spend of a chain shows as one run. A management tool can read the registry, but it cannot change it. |
| D39 | Approvals. Install gate: the operator approves a build before `register` (Install + run once, Install + always allow, Show code·tests·log, Reject). Use gate: the CLI asks before each `use_tool`, except when "always allow" is set for the permission hash in `.frank/permissions.json`. The gate of a big tool lists its sub-tools. The CLI never asks about a sub-tool. `/allow` lists and revokes entries. | Real operator control. A change of permissions makes a new permission hash. Thus the operator sees the gate again. |
| D40 | Python 3.13 everywhere. uv is the only package manager, on the host and in the image (pinned 0.12.24). One uv workspace at the repo root, with one `uv.lock`. shedd and the SDK use only the standard library. No pip, venv or apt Python packages. Replaces PR-4. | One language and one lock file. Fewer dependencies in the container. |
| D41 | P4 security has two parts: static AST checks and a `deepseek-flash` reviewer. The static checks refuse `subprocess`, `eval`, `exec`, environment access, host paths, and a `shed.call` name that is not a literal in `uses`. `deepseek-v4-pro` is a switch, not the default (the reviewer model: replaced by D52). A reject gives one more coder iteration. Replaces PR-8. | Static checks are free and deterministic. Flash is cheap. The runtime socket also enforces `uses` (D38). |
| D42 | Invocation output and records. When the JSON result is more than 6 KB, shedd writes it to `/work/out/` and returns a preview and the path. The `invoked` event records the ID, the status, the duration and the error, but not the arguments. | Large outputs do not fill the context. The agent can read the file with `ws_read`. Arguments can contain personal data. |
| D43 | Package tiers. Tier A (common web, data and test packages) is in the image. P1 finds packages with `pkg_search` (top 8 from `packages.json`: 908 packages, 21 categories). P3 installs one with `request_package`: catalog names only, with `uv pip install --system`. | The common path is fast. Each extension has a known name and a record. |
| D44 | The toolshed network is open. Tools that need a third-party key are not in the prototype. PR-14 (key injection) stays LATER. | Tools can get public data. No new credential enters the system. |
| D45 | The meter listens on a unix socket, `.frank/run/meter.sock` (directory 0700, socket 0600). The container mounts the directory at `/run/meter`. Tools never reach the meter directly. Replaces the TCP port `:7701` of the plan. | Spike, 2026-10-08: `host.containers.internal` reaches a host listener only on `0.0.0.0`, which puts the meter on the LAN. With these modes, container root (shedd) connects and the tool user gets EACCES. |
| D46 | shedd operates as container root. The runner executes all agent code as the tool user (uid 1000) with `setpriv --no-new-privs --clear-groups`, `prlimit`, `timeout` and `env -i`. `make toolshed-up` makes `.frank/admin.token` (0600) one time and gives it to the container as one environment variable. shedd removes it from its environment at start. | Spike, 2026-10-08: `setpriv` and `prlimit` operate in rootless Podman. The tool user cannot read the tool database, the admin token or the meter socket. |
| D47 | No seed tools. No person and no script puts a tool into the real tool database. Each version comes from a Big Chef build through the install gate. Test fixtures go only into a temporary database. Replaces D4. | Brief: seeded code is "the one unforgivable fake". The registry is empty before the first run. |
| D48 | Roles and models. The main agent, P1 plan and improve planning use `deepseek-v4-pro` with effort `high`. P2 tests use `deepseek-flash` `low`. P3 code uses `deepseek-flash` `high`, and `deepseek-v4-pro` `high` after the first red test run or smoke failure. P4 security uses `deepseek-flash` `high`. `shed.llm` uses `deepseek-flash` `low`. Clamps: plan and code 32768 tokens. Each role has an env override (contract §4). The model table of §7 is replaced. Replaced by D52 (the models and efforts) and D54 (the clamps). | Strategic steps need the strong model. The coder used all 16384 tokens 7 times and gave no `tool.py`. |
| D49 | The Big Chef builds in parallel. It installs the deps first, one at a time. Then 4 threads make each new small tool and the entry tool. The live smoke run of the entry waits for the small tools. One P1 turn runs its probes in parallel. Adds to D36. | A serial build took more than 4 minutes. |
| D50 | Improve mode. `big_chef` `repair_of` takes `{tool, invoke_id, problem}`. shedd accepts an ok invocation only from the same session and only with a problem. A repair of a failed invocation is refused when the error is a `ValueError` (an argument error). The CLI adds the recorded args. The CLI marks problems in a result (empty results, a field that is null in all rows, warnings). The new version n+1 keeps the old tests and adds tests. Changes the gap rule of D35. | Before, a tool that ran but gave wrong data had no repair path. The agent made a failed call to unlock repair. |
| D51 | Dev mounts. `make toolshed-up` mounts the server, the SDK and `llm.py` read-only from the host tree. `make toolshed-restart` loads new code. The container is made again when its mounts, image or role env change. The data volume stays. | The container ran old code after each edit until `make image`. |
| D52 | All roles use `deepseek-v4-pro` with effort `high`: the main agent, P1 plan, P2 tests, P3 code, P4 security and `shed.llm` in tools. Each role keeps its env override (`TALTEMPLA_MODEL`, `TALTEMPLA_EFFORT`, `CHEF_<ROLE>_*`, `SHED_TOOL_MODEL`, `SHED_TOOL_EFFORT`). `shed.llm` sends the full `tool` clamp as `max_tokens`, because the reasoning tokens count in `max_tokens`. Replaces the role table of D48 and the reviewer model of D41. | Operator decision, 2026-10-08. Each step gets the strong model. D54 keeps the calls in the caps. |
| D53 | The main agent runs only big tools. Small tools are building blocks: only big tools call them, with `shed.call` in the container. Code enforces this at two points. (1) The agent lookup (`POST /lookup` with a query) gives big tools only and calculates the fit over big tools only. When no big tool fits but small tools match, the fit is `partial`: the agent calls `big_chef`, and the Chef makes a big tool from the small tools. `lookup(tool=X)` for a small tool gives a short note, not the schema. `explore` and the stored lookup row (for the Chef) keep all grades. (2) `POST /invoke` refuses a small tool with a 400. Nested `shed.call` runs small tools as before. The CLI also refuses a small tool before the use gate. Replaces "Lookup shows big tools first" in D34 and `lookup(tool=X)` for small tools in D35. | Operator test, 2026-10-08: for "find and download 3 images of X", lookup gave fit `good` to the small tool `download_file`. The agent called it directly and invented the image URLs. |
| D54 | Meter fit-to-budget clamp and new caps. Role clamps for `max_tokens`: main 16384, plan 32768, tests 32768, code 32768, security 16384, tool 8192. When the reserve does not fit the budget left at a cap level, the meter decreases `max_tokens` until the reserve fits. The meter refuses (402) only when the reserve does not fit at the floor: 1024 tokens for the role `tool`, 4096 for the other roles. The meter never increases a smaller request. The ledger and `x_meter` record the `max_tokens` that the meter sent. Caps: USD 1.00 for each build, 3.00 for each run, 6.00 for each session, 20.00 in total (Makefile `CAP_*`, `main.py`, `meter.py`). Replaces the caps of D37 and the clamps of D48. | At peak, `deepseek-v4-pro` output costs USD 3.96/M. A reserve for 32768 tokens is about USD 0.13. Without the clamp, four parallel coder calls go over a USD 0.60 build cap, and a tool with `llm_usd` 0.05 gets a refusal for each call. |
| D55 | The Chef wall time is a setting (G3). The build wall time (default 360 s) and the P1 soft limit (default 150 s) come from Makefile `BUILD_SECONDS` and `PLAN_SECONDS` (env `TALTEMPLA_BUILD_SECONDS`, `TALTEMPLA_PLAN_SECONDS`). The CLI sends them in `/chef/build` `options`. The Chef clamps them: wall time 60–3600 s, P1 30 s to the wall time. A bad value gives the default and a trace note, never an error. The first P1 trace line shows the effort and the times. `CAP_SECONDS` and `PLAN_SECONDS` in `orchestrator.py` are only the defaults. | `deepseek-v4-pro` on all roles is slow (D52). A fixed 6 min cap stopped good builds. A setting changes no code. |
| D56 | The operator chooses the options of each build (G2). Before each Chef build, the CLI shows the USD cap, the effort and the wall time, and asks: Start, Change, or Cancel. The effort (`low`, `high`, `max`) replaces the `reasoning_effort` of all Chef roles for that build only; the models do not change. The cap is at most the run budget left. The defaults come from `TALTEMPLA_CAP_BUILD`, `TALTEMPLA_BUILD_EFFORT` and D55. `GATE=auto` uses the defaults without a question. Cancel, Ctrl-C or EOF stops the build before it starts and does not count it. The CLI refuses a build when the run budget has less than USD 0.01. The env overrides of D52 stay. | The operator controls spend and speed before the spend starts. A bad entry keeps the old value. Thus the gate cannot block a run. |
| D57 | The Waiter (G1). After a failed Chef build, the CLI makes one metered call (`deepseek-v4-pro`, `high`, role `waiter`, the run grant). The call reads the trace tail (12 KB or less) and the failure reason, and gives JSON `{cause, advice}`. The CLI shows the cause and asks "Retry the Chef?". Each retry needs the operator's approval: there is no automatic loop. A retry is a new build (new build ID and build grant, the options gate again) with `advice` in `/chef/build`. The Chef puts the advice only in user messages, as data, never in the system prefix. `GATE=auto` makes no Waiter call and no retry. Any error (refusal, bad JSON, Ctrl-C) gives the fallback: the raw failure reason and no advice. The 2-builds limit for each prompt counts one `big_chef` call with its retries as one build. The Waiter is not a tool of the main agent. | A failed build gave only a raw reason. The operator could not see the cause or try again with a fix. One call, one approval for each retry, and the fallback keep the cost known and the run alive. |
| D58 | Build gate fixes (replaces parts of D56 and D57). (1) The default build effort is empty: the CLI sends no `options.effort`, and each Chef role uses `CHEF_<ROLE>_EFFORT` (default `high`). The operator can choose `low`, `high`, `max` or "roles" at the gate. (2) The build cap is at most the run budget left minus USD 0.10 (one Waiter call and the main answer). The CLI refuses a build, without a count, when that room is less than USD 0.10. (3) A P1 time at or over the wall time gets the default share (150/360), at least 30 s. (4) The Waiter `max_tokens` is 16384. (5) A retry that shedd refuses before the stream gives the earlier failure, its cost and its diagnosis. (6) The cost of a failed attempt is the meter's spend on its build grant. | A fixed effort hid the per-role overrides of D52. A build could spend the run budget and leave no money for the answer. A cap under USD 0.10 cannot pay the first P1 call. A P1 limit equal to the wall time never gives the last-turn nudge. Reasoning tokens count in `max_tokens`. A broken stream has no cost line. |
| D59 | Meter: wait, do not refuse (replaces the refusal rule of D54). When a call does not fit, or its fit would decrease `max_tokens` below half of the role clamp, ONLY because of the in-flight reserves of other calls, `Meter.chat` waits on a `threading.Condition` on the meter lock (the wait releases the lock). Each settle, revoke and top-up notifies it; it also checks again each 5 s. Then it fits as in D54. Maximum wait: 600 s in the process (`WAIT_S`), 240 s on the socket (`WAIT_SOCKET_S`, so that the wait and the call stay in the client timeout). After the maximum wait: the D54 clamp or 402. A revoke during the wait gives 401. `x_meter.waited_s` records the wait. The container `shed.llm.chat` timeout is 1500 s. | Build `b-3846f89700` (2026-10-09): four P2 calls started at the same time. Their reserves refused one call at the 4096 floor (ledger row 417) and clamped one to 25202 tokens, while the build had money left. It was one of three failed builds (USD 0.52 together, no tool). A wait costs time, not money. |
| D60 | Build cap top-up (replaces "a 402 on the build cap stops the build" of D54 and "a new build is the only retry" of D57). When a Chef LLM call gets 402 with `cap` = `build`, or the build wall time is over, the Chef emits `cap_hit` and polls `POST /v1/topup` on the meter socket each 2 s for 900 s or less. The CLI asks the operator: "+$0.50" / "+$1.00" (each at most the run budget left minus USD 0.10) or "+5 min" / "+10 min", or "Stop the build" (the safe choice; `GATE=auto` and Ctrl-C give Stop). The CLI calls `Meter.topup`. The meter clamps the raise to the room of the parent chain and the total cap. The container can only read the decision. One request at a time for each build; 2 top-ups for each build (`CAP_TOPUPS`). Denied, no answer, or a 401/404 on the poll gives the old failure. The wait for the operator does not count as build time (the Chef moves `t0`). Run, session and total caps do not ask. | A build cap or the wall time stopped builds with work in progress, also when the run had money and time left. The operator had no choice: the only path was a new build from P1 (three failed builds, USD 0.52, no tool). |
| D61 | Checkpoint and resume (replaces "a retry is a new build from P1" of D57). After P1 the Chef emits a `checkpoint` event with the plan (stage `plan`). It emits one more for each green, approved draft (stage `tool`). `builds_log` keeps them; no schema change. `/chef/build` gets an optional `resume_of`. shedd accepts it only for a `failed` build of the same session and task that has a plan checkpoint; else a trace warning and a normal build, never 400. The Chef runs `check_plan` on the stored plan again. A change in the registry versions or any error gives a normal P1. It reuses a green draft only when `check_register` passes and its manifest is the planned one; else it builds the tool again. The entry smoke run and the Waiter advice stay. The handoff saves each draft again, and the `content_hash` upsert moves it to the new build. Thus `register` keeps its `build_id` check. The Waiter JSON gets `replan` (missing = false). The CLI offers "Resume from the checkpoint" only when the failed build has a plan checkpoint and `replan` is false. | A retry (D57) started again at P1 and built the green tools again. P1 is the slowest phase: in `b-3846f89700` it took about 5 min and USD 0.12 of the USD 0.23. |
| D62 | Transient retry in `llm.py` (changes the retry rule of contract §3). `request_json` retries one time, after 2 s, when no response started: `ConnectionResetError` (also `RemoteDisconnected`), `ConnectionRefusedError`, `BrokenPipeError`. It never retries a timeout, a failure while it reads the body, or a second drop. The 429/500/503 retries do not change. | A dropped connection stopped a call that had cost nothing. A timeout can be billed, so we do not retry it. Accepted risk: a drop after the call was forwarded bills it two times, once. |
| D63 | Chef spinner and planner lines. (1) In interactive mode, the CLI shows a rich status during the build stream: "chef <phase> <tool> · <s> s since the last step · $<cost> · <elapsed>/<cap> min". The text is calculated at each refresh. It stops before each gate prompt and before the Waiter, and on each exit path (Ctrl-C too). `--once` has no spinner. A rich error gives no spinner, never a failed build. (2) `plan.md` gets two lines: the entry solves the general need and takes the subject of the task as an argument; extend a similar registry tool before you add a near-duplicate small tool. | (1) One P1 call took about 2 min with no output (ledger row 416). The operator could not see the phase, the time or the cost. (2) Planned entries were narrow to one task, and similar small tools were added again. |
| D64 | Review fixes for D59–D61 (replaces the socket wait of D59, the read that consumes the decision in D60 and the resume condition of D61). (1) The socket wait is 600 s or less (`WAIT_SOCKET_S`). A socket caller can make it shorter with the header `X-Taltempla-Wait` (seconds). The Chef sends the wall time that is left. Role `tool` without the header does not wait. (2) A 402 on the build cap with `spent + need <= limit` comes only from the reserves of other calls. The Chef sends the same turn again (3 times or less, 2 s between them) and does not ask for a top-up. (3) When a worker fails, the Chef does not wait for the other workers. `failed` ends the stream, the CLI revokes the grant, and a call that waits in the meter gets 401 (no paid call). (4) `Meter.topup` returns the stored decision. Each decision has a `seq`. `POST /v1/topup` does not consume the decision. The Chef ignores a `seq` that it used before. The CLI shows the raise that the meter applied. (5) The CLI offers Resume only after a `failed` event from shedd. | Code review of D59–D61: a 402 from reserves used a top-up; a waiting call made a paid call after its build failed; a lost poll reply lost the answer of the operator; a tool call waited past the tool deadline; a broken stream offered a resume that shedd refused. |

## 15. Demo
1. `make toolshed-reset && make shed` shows an empty tool database.
2. **Session 1:** "Cheapest house in Brno-venkov under 8M CZK." Lookup gives fit `none`. The Big Chef trace shows the tests fail, then pass, and the security verdict. The operator approves at the install gate and the use gate. The answer has the listing URLs. `/cost` shows the spend.
3. **Session 2 (a new `make run`):** "5 cheapest used Octavia combi in Prague." Lookup gives fit `partial`. The Big Chef makes a big tool from the session 1 small tools and does not build them again. `/cost` shows "saved $X".
4. **Session 3:** "Which tools cost most, and which fail most?" The Big Chef builds `toolshed_insights`. It reads the registry, read-only.
5. A real failure makes a repair: version 2. Then `/rollback` moves back to version 1.
6. `/cost` shows the cost of each run, of the session and in total, and compares it with the change of the DeepSeek balance.

In the video, we speed up the waits. We never cut a failure.

## 16. Prototype scope and plan
| In the prototype | LATER |
|---|---|
| CLI: agent loop, meter, gates, status line, `/shed`, `/cost`, `/rollback`, `/allow` | TUI polish, voice (ElevenLabs) |
| shedd: admin API, lookup, runner, runtime socket, `register`, `rollback`, `history` | `network` and `files` permissions for each tool, VPS, micro-VM |
| Big Chef P1–P5 and repair | `deepseek-v4-pro` reviewer as the default |
| Two grades, chaining, agentic tools through the meter | Key injection (PR-14), tools with third-party keys |
| Package tiers, `pkg_search`, `request_package` | Import and export of tools, rollback of the full toolset |
| Makefile (§11), a small `make test` | Live boundary tests |

`make test` (host, no network, no container): meter caps, usage, 401; `register` refusal; a call outside `uses`; depth and subcall limits; append-only triggers; the static check catches `subprocess` and a dynamic `shed.call`.

| Time | Work | Exit |
|---|---|---|
| 0:00 | Containerfile, `tiers.py`, image build in the background | — |
| 0:10–1:00 | CLI core: client, meter, ledger, meter socket. Toolshed spike (done: D45, D46). | A chat with a cost line |
| 1:00–2:00 | shedd core: tool database, FTS, lookup, runner, runtime socket, `invoke`, `register`, `rollback` | A chained, metered fixture invoke (temporary database) |
| 2:00–2:40 | CLI tools, gates, status line, commands | M1: lookup → use_tool → chain → cost |
| 2:40–4:40 | Big Chef P1–P5, install gate | M2: `fetch_page` built. M3: the real-estate tools built and used in one session. |
| 4:40–5:40 | Prompt tuning, savings, repair, read-only registry | The second build uses the small tools again |
| 5:40–6:50 | Dry runs, fixes, lookup thresholds | The script passes two times |
| 6:50–8:00 | Docs, `demo-reset`, video | — |
