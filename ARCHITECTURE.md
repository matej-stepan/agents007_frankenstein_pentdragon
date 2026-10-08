# Taltempla architecture

Status: draft v6, 2026-10-08. Language: ASD-STE100. This is a plan, not an implementation.
Scope: a prototype in 9 hours (§16). Brief: [SUBJECT.md](SUBJECT.md). Open questions: [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md). Raw research: [research/](research/).
**PROPOSAL** = not a decision yet (§13). **LATER** = not in the prototype. **UNVERIFIED** = a test must confirm the fact.

## 1. Modules
The **CLI** is an agentic CLI on Pi. It holds the DeepSeek key and makes all LLM calls. It never executes generated code.
The **toolshed** is one long-lived rootless Podman container. It executes code and keeps the tool database. It has no credentials.

## 2. Terms
| Term | Definition |
|---|---|
| Admin API | The HTTP API of the toolshed server for CLI code (§8). It needs the admin token. The model cannot reach it. |
| Admin token | A random local secret. Only the CLI and the toolshed server know it. It is not a credential. |
| Agent channel | The MCP endpoint of the toolshed server. It gives the main agent one tool for each active version. |
| Build | One forge sequence for one gap (B1–B8). |
| Build log | The record of each build: the triage result, each iteration, each test run, the review and the gate decision. The CLI owns it. |
| Cap | A limit that code enforces on USD, builds, iterations, turns or time (§10). |
| Catalog | The CLI copy of the `list` result: the name, description and schema of each active version. |
| CLI | The module that the operator uses: a Pi package that the launcher starts. |
| Content hash | The hash of the code, the manifest and the test set of a draft. |
| Credential | A key for an external service, for example the DeepSeek key. |
| Draft | The files of a tool that the toolshed did not register. |
| Exchange directory | The host directory `./workspace`, with `in/` and `out/`. The toolshed mounts it. Files go between the modules only through it. |
| Forge | A CLI tool that makes a version from a gap (§9). |
| Fresh context | A new main agent context in the same run. It has the prompt, the gap list, the file references and a handoff note. |
| Gap | A capability that a task needs and that the toolset does not have. It has an ID and a spec: purpose, interface, examples. |
| Gap list | The gaps of the current run and their resolutions. |
| Gap pass | A nested call before each reply. It has a low threshold, on purpose: when it is not sure, it reports a gap. |
| Gate | The step where the operator approves or rejects a draft. |
| Governor | The CLI component that enforces the caps and keeps the ledger. |
| Guard | The CLI component that stops Pi if a tool outside the allowlist is present. |
| Iteration | One generator attempt in a build. |
| Launcher | The `make run` command. It starts Pi with a fixed configuration (§6). |
| Ledger | The record of the USD cost of each LLM call. |
| Main agent | The Pi agent loop that answers the operator. |
| Management tool | An agent tool that reads the tool database and returns information. It cannot change the tool database. |
| Nested call | An LLM call that CLI code makes outside the main agent loop. |
| Notice | The MCP message that tells Pi that the toolset changed. |
| Operator | The person at the terminal. |
| Origin | The source label of a version: `seed` (the team wrote it) or `agent`. |
| Repair record | A gap that a failed tool call makes. |
| Resolution | The end state of a gap: `built`, `failed`, `covered`, `out_of_scope` or `skipped`. |
| Run | One operator prompt, until the main agent stops. |
| Session | One Pi process, from `make run` to exit. A session contains one or more runs. |
| Test set | The tests that the test writer wrote for a draft. |
| Tool | A capability with a manifest (purpose, interface, examples, stack, permissions), code and a test set. |
| Tool database | The SQLite file in the toolshed. The brief calls it the registry. |
| Tool user | The low-privilege user in the toolshed that executes all generated code. |
| Toolset | The active version of each tool. |
| Toolshed | The module that executes code and keeps the tool database. |
| Toolshed server | The trusted process in the toolshed. It owns the admin API, the agent channel and the tool database. |
| Triage | The first nested call of each build. It decides: an available tool covers the gap, or the forge builds. The build log records the result. |
| Version | One registered state of a tool. A content hash identifies it. It has an origin and a parent version. |
| Workspace tools | `ws_read` and `ws_write`: main agent tools for text files in the exchange directory. |

## 3. Rules
1. Generated code executes only in the toolshed, as the tool user. It never executes on the host.
2. The CLI has no host code execution: no `bash`, no `codemode`, no operator shell, no writes outside the exchange directory.
3. The toolshed has no credentials. It makes no LLM calls.
4. The CLI makes all LLM calls. All LLM calls go to the DeepSeek API.
5. The toolshed registers a draft only after its test set passes and the operator approves its content hash. The build log shows each test run.
6. The main agent can call only registered tools, `forge`, `resolve_gap` and the workspace tools. Only CLI code uses the admin API.
7. Each tool declares its permissions. The gate shows each new permission to the operator.
8. Code enforces caps on USD, builds, iterations, turns and time.
9. Each version has an origin and a parent version. Only `make seed` writes the origin `seed`.
10. The tool database keeps all versions and records. History only goes forward. The operator can roll back a tool.
11. Each gap comes from a task. The forge accepts only a gap ID from the current run. Each gap gets a resolution.
12. Capabilities can grow, authority cannot. Tools can read the tool database, but they cannot change it. Only the operator changes it: at the gate or with `/rollback`.
13. The agent never sees a key. No key enters the LLM context, the build log, the tool code or the toolshed.

## 4. Module boundary
| Responsibility | Owner |
|---|---|
| Operator interface: prompts, gate, commands, status | CLI |
| The DeepSeek key and all LLM calls | CLI |
| Gaps, triage, specs, resolutions, review and the approval decision | CLI |
| Caps, ledger and build log | CLI |
| The catalog and how Pi shows the toolshed tools | CLI |
| Code execution, packages and the limits for each execution | Toolshed |
| Tool database: versions, records, active pointers, rollback | Toolshed |

| Channel | Direction | Transport | Caller | Content |
|---|---|---|---|---|
| Admin API | CLI → toolshed | HTTP on 127.0.0.1, with the admin token | CLI code only | The operations in §8 |
| Agent channel | CLI → toolshed | MCP streamable HTTP on 127.0.0.1 | Main agent, through Pi MCP | One MCP tool for each active version |
| Notice | Toolshed → CLI | MCP `notifications/tools/list_changed` | Toolshed server | After `register` and `rollback` |
| Files | Both | Exchange directory | Toolshed, workspace tools | File references |

**Never crosses:** credentials, LLM calls and requests for host code execution.

## 5. Boundary enforcement
**E** = code or configuration enforces the rule. **CONV** = a convention only, in the prototype.

| # | Rule | Mechanism | Status |
|---|---|---|---|
| E1 | 1 | The toolshed server executes all generated code as the tool user, with a timeout and resource limits. The container is rootless. | E |
| E2 | 2 | The launcher removes `bash`, `codemode`, `read`, `write` and `edit` with `--exclude-tools`. | E |
| E3 | 2, 6 | At `session_start` and `turn_start`, the guard compares the Pi tool list with an allowlist. Another tool stops Pi. | E |
| E4 | 2 | A `user_bash` handler refuses the operator `!` shell. The workspace tools refuse each path outside the exchange directory. | E |
| E5 | 3, 13 | `make toolshed-up` starts the container with no host environment, no home directory mount and no sockets. It mounts only the exchange directory and the data volume. | E |
| E6 | 4, 13 | The launcher starts Pi with only the DeepSeek key, `PATH` and `TERM`. A `model_select` handler refuses each provider other than `deepseek`. | E |
| E7 | 5, 6 | `register` needs the admin token, a passed test run on the content hash and an approval that names the content hash. The tool user cannot read the admin token. | E |
| E8 | 5 | The forge gives the generator no operation that changes the test set. Only the test writer writes it. | E |
| E9 | 7 | The gate shows each new permission. The toolshed does not apply permissions. | CONV; LATER: network for each tool |
| E10 | 8 | The governor enforces each cap (§10). The forge is sequential. A cap can overshoot by one call (D24). | E |
| E11 | 9, 10 | Only `make seed` writes `seed`. The admin API has no delete operation. | E |
| E12 | 11 | `forge` accepts only a gap ID from the gap list of the current run. | E |
| E13 | 12 | The tool user can read the tool database file. It cannot write it. | E |
| E14 | 13 | The agent has no host shell and no file access outside the exchange directory (E2, E4). The ledger and the build log record no request headers. | E |

LATER: static code checks (`make check`) and live boundary tests (`make test-boundary`).

## 6. CLI module
The launcher starts Pi 1.1.0 with a fixed configuration. The Pi agent directory is `.frank/agent`: separate settings, prices and sessions.
Pi loads only the MCP support and `cli/`. It loads no built-in tools and no project-local configuration. The DeepSeek key is only in the launcher environment.

| Component | Pi mechanism |
|---|---|
| Guard | `session_start` and `turn_start` with `pi.getAllTools()`; `ctx.shutdown()` |
| Workspace tools | `pi.registerTool`: `ws_read` and `ws_write`, with jailed file operations |
| Toolshed client | `make run` calls `health`. At `session_start`: `list`, then `pi.registerMcpServer`. If the toolshed does not answer, the CLI stops. |
| Gap pass | `before_agent_start`: one nested call. The plan and the specs enter the context as text. |
| Forge | `pi.registerTool("forge")`, sequential. `execute()` does B1–B8 as nested calls with fresh contexts. |
| Triage | The first nested call of `forge`. The build log and the ledger record the result. |
| `resolve_gap` | `pi.registerTool`: the main agent marks a gap `covered` or `out_of_scope`, with a reason. |
| Gate | `ctx.ui.custom()` or `ctx.ui.confirm()`. Without a UI, there is no approval. |
| Governor | `input`, `turn_start`, `turn_end`, `message_end`, `tool_call` on `forge`, and the nested-call wrapper |
| Build log | `pi.appendEntry()` and a file for each build in `.frank/runs/` |
| Repair, settle | `tool_result`: a failed toolshed call makes a repair record. `agent_before_settle`: a gap without a resolution gets one more turn. |
| Operator view | Commands `/shed`, `/gaps`, `/skip`, `/cost`, `/rollback`. The status line shows the USD left. |

## 7. Inference
The CLI uses only the DeepSeek API, through the built-in `deepseek` provider of Pi. The OpenAI SDK is not necessary (PR-7).

| Role | Model | Thinking | Context | Output |
|---|---|---|---|---|
| Main agent | `deepseek-flash` | `high` | Pi session | Text and tool calls |
| Gap pass | `deepseek-flash` | off | Prompt, recent turns, catalog | Named tool `report_plan` |
| Triage | `deepseek-flash` | off | Spec, catalog | Named tool: `covered` with tool names, or `build` |
| Stack pass | `deepseek-flash` | `low` | Spec, runtime packages | The manifest stack, packages and permissions |
| Test writer | `deepseek-flash` | `high` | Spec and examples, never code | Test set |
| Generator | `deepseek-flash` | `high` | Spec, manifest, test set, results | Forge tools: `put` (code only), `exec`, `test`, `request_package` |
| Reviewer | `deepseek-v4-pro` (PR-8) | `high` | Spec, manifest, code, build log | Tool `submit_verdict`. No verdict means reject. |

1. Thinking mode rejects a forced `tool_choice`. Thus forced output uses thinking `off`.
2. DeepSeek rejects a request without the earlier `reasoning_content`. Thus the forge loop returns each assistant message unchanged.
3. Cost of one call = cache-hit tokens × hit price + cache-miss tokens × miss price + output tokens × output price.
4. The ledger is the authority for cost. The wrapper settles each nested call. `message_end` settles each main agent message.

## 8. Toolshed module
The toolshed is one long-lived rootless Podman container. `make toolshed-up` starts it. `make toolshed-down` stops it and keeps the data volume.

| Part | Description |
|---|---|
| Toolshed server | The trusted process. It owns the admin API, the agent channel and the tool database. |
| Tool user | It executes `exec`, `test` and `invoke`, with a timeout, a memory limit and a process limit. It cannot read the admin token or write the tool database. |
| Runtime | Python 3.13, `uv` and system packages, for example `pandoc`, `typst` and fonts. A tool is one Python file (D21). |
| Packages | The manifest is the single source. `install` makes a separate environment for each tool. The network is on. |
| Tool database | SQLite in the data volume: versions (code, manifest, test set, origin, parent), test runs with full logs, approvals, events and active pointers. |
| Rollback | It moves the active pointer of one tool and records an event. |
| Test minimum | One case for each manifest example and one error case or more. |
| File references | A manifest input or output of type `file` is a path in the exchange directory. Thus the output of one tool can be the input of the next tool. |

| Operation | Function |
|---|---|
| `health`, `list`, `history` | Return the status, the tool list with schemas, or all records of one tool. |
| `draft`, `put`, `get`, `diff` | Make a draft (empty or from a parent version). Move files into or out of it. Compare it with its parent. |
| `install` | Install the packages of the manifest. |
| `exec` | Execute draft code. Return the exit code, stdout, stderr, the duration and the output files. |
| `test` | Execute the test set. Record a test run with the full log. Return a report and the test run ID. |
| `register` | Make a draft a version (E7). Set the active pointer. Send the notice. |
| `rollback` | Make an earlier version active. Send the notice. |
| `invoke` | Agent channel only. Execute the active version of a tool. Record an `invoked` event. |

## 9. Sequences
Steps: **S** = start, **1–7** = run, **B** = build, **R** = repair.

| Step | Module | Action |
|---|---|---|
| S1 | Toolshed | `make toolshed-up` starts the container and the toolshed server. |
| S2 | CLI | `make run` calls `health` and starts Pi. The guard checks the tool list. The catalog loads. Pi connects the agent channel. |
| 1 | CLI | The operator sends a prompt. The governor refuses it if a cap has no USD left. |
| 2 | CLI | The gap pass compares the prompt with the catalog. Its plan and specs enter the context. The gap list records the IDs. |
| 3 | CLI → Toolshed | The main agent calls registered tools. Each call is an `invoke`. Output files return as file references. |
| 4 | CLI | For each gap, the main agent calls `forge` (B1–B8) or `resolve_gap`. |
| 5 | CLI | A failed tool call makes a repair record. The main agent calls `forge` with it (R1). |
| 6 | CLI | The main agent completes the task. If a gap has no resolution, the settle check requests one more turn. |
| 7 | CLI | The run stops. The governor writes the run cost to the ledger and the status line. |
| B1 | CLI | The governor checks the caps. |
| B2 | CLI | The triage compares the spec with the catalog. The build log records the result. If the result is `covered`, the forge records the resolution and stops. |
| B3 | CLI | The stack pass writes the manifest. The test writer writes the test set from the spec. |
| B4 | CLI → Toolshed | The forge calls `draft`, then `put` with the manifest and the test set, then `install`. |
| B5 | CLI ↔ Toolshed | The generator writes the code and calls `exec` and `test`. A failure starts a new iteration, up to the cap. The build log records each test run. |
| B6 | CLI | The reviewer examines the code, the manifest and the build log. A reject starts one more iteration if the caps permit. |
| B7 | CLI | The gate shows the manifest, the new permissions, the code and test diffs, the parent version, the build log, the verdict and the cost. The operator approves or rejects. |
| B8 | CLI → Toolshed | Approval: the forge calls `register` and records `built`. The run continues in a fresh context (D23). Reject: the forge records `failed`. |
| R1 | CLI ↔ Toolshed | The forge makes a draft from the active version. The test writer adds the failed input as a test. If that test passes, the forge stops. If not, the build continues at B5. |

## 10. Caps and cost
| Cap | Default | Scope | Enforcer |
|---|---|---|---|
| USD | 0.50 / 2.00 / 5.00 / 20.00 | Build / run / session / total | Governor |
| Builds / iterations | 3 / 5 | Each run / each build | Governor / forge |
| Agent turns, run time | 40, 30 min | Each run | Governor |
| Time, memory, processes | 60 s, 1 GiB, 256 | Each `exec`, `test`, `invoke` | Toolshed server |

The ledger keeps the total for all sessions. When a cap stops a run, the CLI shows the ledger.

## 11. Project structure and Makefile
In git: `Makefile`, `cli/` (Pi package, launcher, governor), `contract/` (operation and manifest schemas for both modules), `toolshed/` (`Containerfile`, server), `seeds/`, `tests/`.
Not in git: `workspace/` (the exchange directory) and `.frank/` (CLI state: agent directory, sessions, ledger, build logs).

| Target | Function |
|---|---|
| `deps`, `build`, `install` | Install Pi, Node packages and Podman. Build the CLI package and the toolshed image. Write `.frank/agent`. |
| `test` | Execute the unit and contract tests. |
| `toolshed-up`, `-down`, `-reset`, `-shell` | Start the toolshed. Stop it and keep the data. Delete all data. Open a shell as the tool user. |
| `seed`, `run` | Load the seed tools with origin `seed`. Start the CLI through the launcher. |

## 12. Stack
| Layer | Choice | Status |
|---|---|---|
| CLI | Pi 1.1.0 (`@earendil-works/pi-coding-agent`): a local Pi package and a launcher | D1 |
| Inference | The built-in `deepseek` provider of Pi: `deepseek-flash`; `deepseek-v4-pro` for the reviewer | D14, PR-7, PR-8 |
| Toolshed | One rootless Podman container, SQLite, Python 3.13 tools with `uv` | D21, D25, D28, D29 |
| Toolshed server | TypeScript, with the MCP TypeScript SDK | PROPOSAL PR-4 |

## 13. Proposals
| # | PROPOSAL | Rationale |
|---|---|---|
| PR-4 | The toolshed server is in TypeScript. | It shares the `contract/` schemas with the CLI. |
| PR-5 | A launcher and a guard, not a custom binary. | The stock Pi TUI stays. |
| PR-6 | No codemode. The toolshed tools are `direct`. | Codemode executes model JavaScript on the host. |
| PR-7 | The Pi `deepseek` provider for all calls, not the OpenAI SDK. | One usage format and one point for the governor. |
| PR-8 | The reviewer uses `deepseek-v4-pro`. Fallback: `deepseek-flash` with thinking `max`. | A different model has different blind spots. |
| PR-10 | A dedicated DeepSeek account with a small prepaid balance. | DeepSeek has no spend limit. An empty balance is the only provider stop. |
| PR-11 | A synthetic CV persona for the demo. | DeepSeek keeps data in the PRC and can use it for training. |
| PR-14 | LATER: key injection. A proxy adds a key to the outbound request of a tool. The tool code never sees the key. | The mentor prefers injection (D27). |
| PR-15 | Twist: a price tag on each tool. `/shed` shows the build cost and the USD that each reuse saved. The run summary shows the cost with and without the toolset. | It uses the ledger. It shows that the toolset pays for itself. |
| PR-16 | Twist: Frankenstein roles. The generator is "the Doctor", the test writer is "Igor", the reviewer is "the Mob". | Cheap: prompts and theme only. Easy to remember in the video. |

## 14. Decision log
| # | Decision | Reason |
|---|---|---|
| D1 | The CLI is an agentic CLI on Pi 1.1.0: a local Pi package that a launcher starts. A reskin comes later. | Small core, hooks for the gate and the caps, MCP, a DeepSeek provider. |
| D2 | Replaced by D13. | — |
| D3 | Generated code never executes on the host. | Brief hard rule 1. Also, the gaps stay real. |
| D4 | The team can add seed tools. The tool database labels them `seed`. Seed tools are generic and outside the demo task path. | The brief forbids only false claims of generation. |
| D5 | The forge has a stack pass. It writes the manifest stack and permissions. | Each tool gets a good stack. |
| D6 | The gap pass operates before each reply. It has a low threshold. | A false gap costs little, because the triage, the forge and the gate are strict. |
| D7 | The governor records the USD cost of each run. The ledger is the authority. | The organizers asked for it. |
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
| D23 | After `register`, the main agent continues the run in a fresh context. The fresh context has the new tool. | The new tool is available in the same run. |
| D24 | A USD cap can overshoot by one LLM call. We accept this. | A simple governor. |
| D25 | Development and the demo use the dev machine. `make deps` installs rootless Podman. | No daemon, no socket, a built-in timeout. |
| D26 | The first version is a minimal prototype that meets the brief (§16). | Little time. We extend the prototype after the hackathon. |
| D27 | Mentor answer (rwngwn, 2026-10-08): a remote sandbox is not necessary. The agent must not see keys directly. Ideally, the system injects keys. | It answers brief hard rule 1. |
| D28 | The toolshed is one long-lived rootless Podman container. The toolshed server is in it. Generated code executes as the tool user. | Dead simple. Replaces D19 (a new container for each call). |
| D29 | The tool database is SQLite only. Rollback moves an active pointer. | Dead simple. Replaces PR-3 (git and SQLite). |
| D30 | The triage is a separate step in each build. The build log records its result. | The operator wants the triage visible. |
| D31 | Admin API and agent channel use HTTP on 127.0.0.1. The admin API needs the admin token. | Simple. The token stops generated code from calling `register`. |

## 15. Demo
1. `/shed` shows the tool database before the run. It contains no tools, or only generic seed tools.
2. **Session 1:** "Make me a CV from my notes." The forge makes tools. The build log shows each test run, with the failures. The operator approves at the gate.
3. **Session 2 (a new `make run`):** "Change my CV for this job advertisement. Write a cover letter." The triage records `covered` for the session 1 tools. The forge makes one new tool.
4. A tool fails with new input. The forge repairs it and makes version 2. The operator rolls back to show the rollback function.
5. **Session 3:** "Which of my tools fail most often?" The gap pass reports a gap. The forge makes a management tool. It reads the tool database and gives the answer.
6. `/cost` shows the USD cost of each run, of the session and in total.

## 16. Prototype scope and plan
| In the prototype | LATER |
|---|---|
| Launcher, guard, workspace tools | Reskin of the Pi TUI |
| Gap pass, triage, forge roles, gate | Discovery hook: agent tools improve the gap pass |
| Governor: caps, ledger, build log | `make check`, `make test-boundary` |
| One toolshed container, SQLite tool database, rollback | Network and limits for each tool, VPS, micro-VM |
| Operations in §8 | Import, export and check tools for the tool database |
| Commands: `/shed`, `/gaps`, `/skip`, `/cost`, `/rollback` | Key injection (PR-14), voice (ElevenLabs) |
| Demo steps 1–6 (§15) | Rollback of the full toolset |

| Hours | Work |
|---|---|
| 0–1 | Launcher with DeepSeek, guard, workspace tools. Toolshed container with `exec`, `test`, `install` and the tool database. |
| 1–4 | Forge: triage, stack pass, test writer, generator loop, reviewer, gate, `register`. Agent channel. |
| 4–5 | Gap pass, governor, ledger, build log. Commands `/shed`, `/cost`, `/rollback`. |
| 5–6 | Composition in a fresh session, repair, management tool. |
| 6–7 | Twist (PR-15, PR-16). |
| 7–9 | Dry runs, video, submission. |
