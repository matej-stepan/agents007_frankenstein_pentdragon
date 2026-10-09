# Taltempla handoff

Status: 2026-10-08, 21:25 MDT. Language: ASD-STE100.
Last commit: `cf18d25` on `main`, pushed to `origin` (passes 2–4, D52–D64). After it: a fresh state (`make demo-reset`, backup in `.frank/backup-20261008-212404/`) and this update.
Read this file first. Then read `contract/INTERFACES.md` (the binding interfaces) and `ARCHITECTURE.md` §14 (decisions D32–D63).

## 1. Summary
- The v7 prototype operates end to end with live DeepSeek calls.
- The agent starts with no tools. It finds tools with `lookup`. It builds missing tools during the run with the Big Chef.
- Generated code executes only in the toolshed container, as uid 1000. The container has no key.
- The registry keeps versions. An improve operation makes version n+1 and keeps the old tests.
- The main agent runs only big (task-level) tools. Small tools are building blocks that only big tools call in the container (D53).
- All LLM roles use `deepseek-v4-pro` with effort `high` (D52).
- Before each build, the operator chooses the cap, the effort and the wall time (D55, D56). After a failed build, the Waiter tells the cause and the operator can retry with its advice (D57).
- All offline tests pass. The in-container selftest has 0 failures.

## 2. Operator rules for each session
1. Testing time is 10 minutes or less for each change pass. Use offline tests and a maximum of one short live run.
2. Do not trim or compact the context. "Lean context" means only this: the harness has no preloaded tools.
3. Do not lock the agent to one language. The language boundary is open (§11, item 1).
4. All LLM roles use `deepseek-v4-pro` with effort `high`: the main agent, each Chef phase and `shed.llm` in tools.
4a. The main agent runs only big tools. Code enforces this in `lookup` and `/invoke`. Do not weaken it.
4b. When you add a mechanism, use the leanest one that cannot fail catastrophically. It must never stop a run, block a build or start a paid loop.
5. Tools must be generic and reusable. Prefer "extend" or "improve" to a new narrow tool.
6. Never write, edit or seed tool code. Only the Chef writes tools (brief: "the one unforgivable fake").
7. Never print `.env` or the key. Never pass `.env` or the host environment into the container.
8. Commit or push only when the operator tells you to.
9. Do not use `make -n` (see §10).
10. Work only in this directory. Do not read `../Taltempla/`. Ignore `.claude/worktrees/`.

## 3. Start procedure
| Step | Command | Result |
|---|---|---|
| 1 | `make doctor` | Six checks: uv, rootless podman, subuid, key in `.env`, image, health. Each failure shows the fix. |
| 2 | `make toolshed-up` | Starts the container (idempotent). It builds the image if it is missing. |
| 3 | `make run` | Starts the interactive CLI. |
| 4 | `make shed` / `make cost` | Shows the registry / the ledger. |
| 5 | `make demo-reset` | Backs up the registry, ledger and `workspace/out` to `.frank/backup-<ts>/`, then clears them. |

Headless test run: `TALTEMPLA_GATE=auto TALTEMPLA_CAP_RUN=0.4 uv run taltempla --once "<prompt>"`. Use `GATE=auto` only for tests.
In the CLI: `/shed`, `/cost`, `/allow`, `/rollback NAME [V]`, `/history NAME`, `/help`. To quit: Ctrl-D, `/exit`, `exit`, or Ctrl-C on an empty line.
After you change server or SDK code: `make toolshed-restart` (about 2 s). The container reads that code from the host through read-only dev mounts (D51). Use `make image` only when `toolshed/Containerfile` or `toolshed/tiers.py` changes.

## 4. Implementation map
### Module A: CLI on the host (`cli/taltempla/`)
| File | Function |
|---|---|
| `main.py` | Entry point `taltempla`. Interactive mode, `--once`, subcommands `shed`, `cost`, `history`, `rollback`, `allow`. Reads `.env`. Starts the meter socket and the session grant. |
| `loop.py` | Agent loop: max 40 turns and max 2 builds for each prompt. Keeps `reasoning_content`. Handles `big_chef` NDJSON, gates, savings, `smoke_problems()` and the improve arguments. |
| `meter.py` | The only key holder. Grants (session, run, build, tool_run), reserve before and settle after, caps, `max_tokens` clamps, ledger, unix-socket server for the container. |
| `llm.py` | Shared stdlib LLM client. The container uses the same file. |
| `prompt.md` | System prompt, about 250 tokens. Do not change it during a session; the prompt cache needs a stable prefix. |
| `waiter.py`, `waiter.md` | The Waiter (D57): one metered call after a failed build gives `{cause, advice}`. Any error gives the raw reason. |
| `gate.py`, `approvals.py` | Build options gate (D56), install gate and use gate. `.frank/permissions.json` keeps "always allow", keyed by the permission hash. |
| `shed_client.py`, `ws_tools.py`, `commands.py`, `ui.py` | Admin API client, workspace jail, slash commands, rich output and status line. |
| `ledger.sql`, `prices.json` | Ledger schema (append-only), peak and off-peak prices. |

### Module B: toolshed in the container (`toolshed/`)
| File | Function |
|---|---|
| `server/shed/app.py` | Admin API on `:7700` (published on 127.0.0.1 only). `/chef/build` streams NDJSON. `check_build()` applies the gap rule and the repair/improve rule. |
| `server/shed/db.py`, `schema.sql` | SQLite at `/data/shed.db`. Append-only triggers. Active pointer. FTS5 index. |
| `server/shed/lookup.py` | bm25 and coverage. Prefix match on name, summary and keywords. Thresholds: `GOOD = 0.6`, `PARTIAL = 0.25`. Each row has `uncovered` terms. |
| `server/shed/runner.py` | `setpriv` (uid 1000), `prlimit`, `timeout`, `env -i`. `run_tests(live=False)` skips `@live` tests during coder iterations. |
| `server/shed/chain.py` | Runtime socket for tools: `shed.call` (only names in `uses`, depth ≤ 3, subcalls ≤ 20), `shed.llm` (`deepseek-flash`, effort low), `shed.registry`. |
| `server/shed/pkgindex.py` | Package catalog search and install (catalog names only). |
| `server/shed/chef/orchestrator.py` | The Big Chef (§5). |
| `server/shed/chef/static_check.py` | AST checks for P4. |
| `server/shed/chef/prompts/*.md` | Chef prompts. They contain no concrete tool names. |
| `server/shed/selftest.py` | Boundary checks inside the container, with a temp DB. |
| `sdk/shed_sdk/` | `Shed` for tool code. `MockShed` for tool tests (it writes to a temp dir, not to `/work`). |
| `Containerfile`, `tiers.py`, `packages.json` | Image: python:3.13-slim, uv 0.12.24, tier A packages, user `tool`. |

### Other
- `Makefile`: the single entry point. `make help` lists the targets. Recipes call sub-makes only through `$(SUBMAKE)`.
- `contract/`: `INTERFACES.md` and `manifest.schema.json`.
- `tests/`: 53 offline tests, about 3 s. `make test` also runs the selftest when the container operates.
- `spikes/`: the container spike and the metered chain check (`make m1`).

## 5. The Big Chef
| Phase | Model and effort | Function |
|---|---|---|
| P1 Plan | `deepseek-v4-pro`, high | Probes the sources (up to 3 probes in parallel for each turn). Gives the entry spec, the reused tools, the extended tools, the new small tools and real data `samples`. |
| P2 Tests | `deepseek-v4-pro`, high | Writes `test_tool.py` from the spec and the sample, never from the code. The tests must fail on a stub. |
| P3 Code | `deepseek-v4-pro`, high | Writes the full file, then SEARCH/REPLACE edits. A reply cut at the token limit is not an iteration. |
| P4 Security | static checks + `deepseek-v4-pro`, high | A static issue is a reject. A reject gives one more coder iteration. |
| P5 Handoff | code | Gate payload: tool tree, permissions, tests n/n, iterations, verdict, cost. |

- New small tools and the entry tool build in parallel (4 threads). The entry smoke run waits for the small tools.
- The entry tool is always grade `big`. It does the whole task from the user's inputs and finds its own URLs and IDs.
- Caps for each build: USD 1.00 (meter), 3 new small tools, 4+1 iterations for each tool, 48 LLM turns, 6 minutes, 150 s for P1. The two time caps are settings (D55): Makefile `BUILD_SECONDS`, `PLAN_SECONDS`, sent in `/chef/build` `options`. The Chef clamps them (wall time 60–3600 s, P1 30 s to the wall time). `CAP_SECONDS` and `PLAN_SECONDS` in `orchestrator.py` are only the defaults.
- A build cap is not a hard stop now (D60). A 402 on the build cap, or the wall time, gives a `cap_hit` event. The operator raises the cap (+USD 0.50/1.00, +5/10 min) or stops the build. Max 2 top-ups for each build; the wait for the answer is not build time.
- Checkpoints (D61): the plan after P1 and each green tool. After a failure, the operator can resume: no P1, no rebuild of the green tools.
- A reply cut at the token limit gets one retry with a "reply now" message (tests, dispute, review), or does not count as an iteration (code).
- Improve mode (D50): `big_chef` with `repair_of {tool, invoke_id, problem}`. The CLI adds the recorded arguments. P2 adds tests only, and the orchestrator appends them to the old tests. The result is version n+1 with `parent`.
- Env overrides: `CHEF_<ROLE>_MODEL`, `CHEF_<ROLE>_EFFORT` (roles: plan, tests, code, security, escalate), `SHED_TOOL_MODEL`, `SHED_TOOL_EFFORT`. The build option `effort` (D56) replaces the effort of all Chef roles for one build.
- Retry with advice (D57): a new build with `advice` in the body. The advice goes only into user messages, never into the system prefix. A resume (D61) adds `resume_of`.

## 6. Caps and limits
| Item | Value | Where |
|---|---|---|
| USD build / run / session / total | 1.00 / 3.00 / 6.00 / 20.00 | `meter.py`, `main.py`; Makefile `CAP_*` |
| `max_tokens` | main 16384, plan 32768, tests 32768, code 32768, security 16384, tool 8192 | `meter.py` |
| Fit-to-budget clamp | When a reserve does not fit the remaining budget, the meter reduces `max_tokens`. It refuses (402) only below 1024 (tool) or 4096 (other roles). The ledger records the `max_tokens` used. | `meter.py` (D54) |
| Wait for in-flight reserves | A call that does not fit only because of other calls' reserves waits: 600 s or less (on the socket: `X-Taltempla-Wait` makes it shorter; the Chef sends the wall time left; role `tool` 0 s). Then the clamp or 402. A Chef 402 from reserves only retries the turn (3 times or less), no top-up. | `meter.py`, `orchestrator.py` (D59, D64) |
| Top-up | 2 for each build (`CAP_TOPUPS`), USD +0.50 / +1.00 (at most the run budget left minus USD 0.10), time +5 / +10 min; poll each 2 s, 900 s or less; the wait is not build time; the meter keeps the last decision with a `seq` | `orchestrator.py`, `loop.py`, `meter.py` (D60, D64) |
| LLM client retry | 429/500/503: 2 retries; a dropped connection with no response: 1 retry after 2 s; a timeout: no retry. Container `shed.llm.chat` timeout 1500 s. | `llm.py`, `shed/llm.py` (D59, D62) |
| Build defaults (D55, D56, D58) | cap USD 1.00 (`CAP_BUILD`) but at most the run budget left minus USD 0.10, effort empty = each role's `CHEF_<ROLE>_EFFORT` (`high`), wall time 360 s, P1 150 s (a P1 time at or over the wall time gets the 150/360 share). The operator can change them at the build gate. `GATE=auto` uses them. | Makefile `BUILD_EFFORT`, `BUILD_SECONDS`, `PLAN_SECONDS` → `TALTEMPLA_BUILD_*`, `TALTEMPLA_PLAN_SECONDS`; `main.py` |
| Build clamps | cap ≤ run budget left (refused below USD 0.01); wall time 60–3600 s; P1 30 s to the wall time; advice 2000 chars | `gate.py`, `orchestrator.py` |
| Waiter | 1 call for each failed build, `max_tokens` 16384 (floor 2048), input 12 KB or less; ask mode only | `waiter.py`, `meter.py` |
| Main agent | `deepseek-v4-pro`, high; 40 turns and 2 builds for each prompt (a build with its retries counts as one) | Makefile `MODEL`, `EFFORT`; `loop.py` |
| Tool execution | timeout 60 s (max 180), address space 2 GiB (max 4 GiB), 512 processes | `runner.py`, manifest `limits` |
| Large result | more than 6 KB goes to `workspace/out/<tool>-<invoke_id>.json` | `app.py` |

## 7. State locations
| Location | Content | In git |
|---|---|---|
| `.frank/ledger.db` | All LLM calls, sessions, savings | No |
| `.frank/admin.token`, `.frank/run/meter.sock` | Admin token (0600), meter socket (directory 0700) | No |
| `.frank/backup-*/` | Backups from `demo-reset` | No |
| `.frank/review-2026-10-08.md` | The review that started the first improvement pass | No |
| Volume `taltempla-data` → `/data` | Registry `shed.db` | No |
| `workspace/` → `/work` | Exchange directory. Tools write to `out/`. | No |

## 8. Current state of the data
- **Registry:** empty (fresh state, 2026-10-08 21:24). The old 7 tools, the ledger, the permissions and `workspace/out` are in `.frank/backup-20261008-212404/` (`shed.sql`, `shed-data.tar`, `ledger.db`).
- **Ledger:** empty. The session before the reset spent about USD 1.10 in total (all ledger rows).
- **DeepSeek balance:** USD 15.35 at 19:10 (from `/user/balance`). The balance updates later than the ledger.

## 9. Verification history
| Test | Result |
|---|---|
| Offline pytest | 58 pass (about 3 s). ruff passes. |
| Selftest in the container | 0 failures: the tool user cannot read the DB, `/proc/1/environ` or the meter socket; the `uses` scope, depth and subcall limits; register rules; append-only triggers. |
| M1: a metered chain | Pass (`make m1`). |
| M2: build and use in one session | Pass (3 live sessions). |
| Demo session 1 (house, Brno-venkov) | Good answers in 2 of 4 runs before the first pass. After the first pass: pass (1 run, 3 min, USD 0.09). Improve mode changed `reality_listing_search` v1 (null prices) to v2 (18/18 tests); the answer had prices and URLs. |
| Demo session 2 (Octavia, Prague) | Pass in 2 runs before the first pass (reuse, "saved" shown). Not done again after the first pass. |
| Operator test drive | Pass. The operator could not quit; this is now fixed. |
| M1 after the second pass | Pass. `shed.llm` operates on `deepseek-v4-pro`. |
| "find and download 3 images of the Charles Bridge in Prague" (second pass) | Partly. No small-tool rows and no invented URLs. But lookup gave fit=good to the big tool `random_image_fetch` (random images only), so the gap rule refused `big_chef`. The agent ran the wrong tool, then started an improve. The P1 plan probed Wikimedia Commons with real data. The check killed the run at 7 min (my timeout) during P2. Cost: main USD 0.031, plan USD 0.112 (5 calls, max 70 s each). |

## 10. Incident (2026-10-08, 18:55)
- An agent used `make -n` as a dry run. With `.ONESHELL`, make executes each recipe that contains `$(MAKE)`, also with `-n`. Thus `toolshed-up` and most of `demo-reset` operated.
- Effect: the ledger moved to a backup, and the container got the dev mounts. No data was lost. The old ledger rows are now in `.frank/ledger.db` again.
- Fix: recipes call sub-makes only through `$(SUBMAKE)`. Rule: do not use `make -n`.

## 11. Open items (priority order)
1. **Language boundary (resolve first).** We must separate three languages:
   - the **answer language**: the language of the operator;
   - the **search language**: the language of the live searches and the query arguments;
   - the **data language**: the language of the source content.
   - Rule: when the operator asks for live searches in their language, the tools search in that language. Example: a Czech prompt about Czech listings gives Czech search terms and Czech sources.
   - Now: `prompt.md` rule 7 says "answer in the language of the user's prompt". No mechanism controls the search language. In one run, an English prompt gave a Czech answer, because the data was Czech. An operator test of an "always English" rule was rolled back: it is not the solution.
   - **Requirement (operator): the leanest implementation that cannot cause a catastrophic failure.** A language error must never stop a run, block a build or start a loop that spends money.
   - Candidate mechanisms (not decided):
     - (a) The CLI detects the prompt language in code and gives it to the model as a run fact.
     - (b) Each tool that searches with free text has an optional `lang` argument (the plan prompt already says "locale is an argument").
     - (c) An operator setting, for example `TALTEMPLA_LANG`, for the answer language.
     - (d) `prompt.md` gets one clear rule for each of the three languages.
   - **Recommendation: (d) + (b) only. No new code paths.**
     1. `prompt.md` rule 7 (about 2 lines): answer in the language of the operator's prompt; use the language of the prompt for free-text search terms, unless the operator asks for another one; give `lang` to each tool that has it.
     2. Chef `plan.md` / `checklist.md` (1 line): a tool that searches with free text has an optional `lang` argument with no default value from the task.
     - Site-specific data (for example Czech category names in a URL) stays as data in the tool. `lang` controls only the free-text search terms.
   - **Do not use** (each adds a failure mode for little gain):
     - a language detector in code: it can misread short mixed prompts (for example "cheapest house in Brno-venkov");
     - a hard `check_plan` rule: a rejected plan can stop a build (this occurred once with a trivial rule);
     - an automatic "write it again" turn: it adds paid turns and a loop risk;
     - a forced single language: the operator rolled back "always English".
   - The worst case of the lean mechanism: one answer or one search in the wrong language. The operator asks again. This is not catastrophic.
   - Exit check: the same request in English and in Czech, with one short live run each. Both runs search in the correct language, and each answer is in the language of its prompt. No offline test is necessary (prompt text only).
2. **Lookup on big tools is too generous.** A big tool with a general name (`random_image_fetch`) gets fit=good for a specific task (photos of a named subject). Then the gap rule refuses `big_chef`, and the agent must run the wrong tool before it can improve it. Lean options: raise `GOOD` for big tools; or accept `big_chef` with fit=good when `need` names the uncovered terms that lookup returns.
3. **Pro on all roles is slow.** P1 alone took several minutes in the last check. Measure one full build. G3 (D55) and G2 (D56) now let the operator set a longer wall time or a lower effort for each build.
4. **Composition of big tools.** The runtime can chain big tools (depth ≤ 3), and the planner can reuse a big tool in `uses`. But `plan.md` does not suggest it, and `MAX_SUBCALLS = 20` (`chain.py`) is too low for a chain of big tools (one big tool can make 8 subcalls). Proposal: one line in `plan.md` and `MAX_SUBCALLS = 60`. The operator postponed this.
5. **Demo session 2:** do it again with the new Chef. Make sure that it extends or reuses the tools and does not make a narrow tool.
6. **Demo steps 4–6 (PLAN.md):** not done. Step 4: a management tool from a task (`shed.registry()`, read-only). Step 5: repair, then `/rollback`. Step 6: `/cost` against the balance change.
7. **Contract gaps in `contract/INTERFACES.md`:** `run_tests(live=…)`, the test-run kind `offline`, `samples` in `submit_plan`, the `parent` field in tree rows.
8. **Image:** the image still contains old server code. The dev mounts replace it at run time. Before the demo recording, do `make image` and then `make toolshed-up`.
9. **`download_file`:** it saves files without an extension (for example `workspace/out/images/cat`). This is a good live example for improve mode.
10. **Stray test file:** done. `demo-reset` moved it to the backup.
11. **Interactive CLI:** the Ctrl-C and exit-word fix has offline tests only. Do a check in a real terminal.
12. **Proposals not done (from the review):** `workspace/out/<session>/` directories.
13. **Live check of G1/G2 (not done).** One short live run in a real terminal: the build gate (Start, Change, Cancel), one failed build, the Waiter cause, and one retry with advice. Make sure that the first P1 trace line shows the effort and the times, and that `make toolshed-restart` loaded the new server code.
14. **Small gaps from G1–G3:** a bad `TALTEMPLA_CAP_*` value still stops the CLI at start (`caps_from_env`). When `grant_info` fails, the build gate uses the run cap as the budget left (the meter still enforces each cap). The role comment in `ledger.sql` does not name `waiter`.
15. **Live check of D59–D61 (not done).** After `make toolshed-restart`, one build in a real terminal with a USD 0.30 build cap. Look for: parallel P2 calls with no `refused` row and no clamp in the ledger (the meter waited), the `cap_hit` prompt (raise one time), then a stop and "Resume from the checkpoint" (the trace shows `resume: plan of …` and `resume: <tool> green from …`, and the install gate registers the tools). Known gaps: a meter wait still counts as build time; the CLI does not read the stream while a prompt is open (workers can block on a full buffer, no deadlock); a resumed tool shows `iterations` 0 and status `new`.

## 12. Implementation goals (operator, 2026-10-08)
Status: G1–G3 are implemented (D55–D57). Offline tests only; no live run yet (§11, item 13).
- **Fourth pass (2026-10-09, D59–D63, offline tests only).** Cause: three failed builds (USD 0.52, no tool). Parallel P2 calls were refused or clamped by the reserves of other calls, and a cap or the wall time stopped correct work. Changes: the meter waits for in-flight reserves (D59); the operator can raise a build cap (D60); a failed build can resume from its plan and green tools (D61); one retry on a dropped connection (D62); a chef spinner and two planner lines (D63). Each part falls back to the old behaviour on bad input or a timeout. Files: `meter.py`, `llm.py`, `loop.py`, `ui.py`, `waiter.py`, `waiter.md`, `orchestrator.py`, `app.py`, `db.py`, `shed/llm.py`, `plan.md`.
- **G1. The Waiter (done, D57).** When a Chef build fails, a Waiter agent reads the build trace and the traceback. It tells the operator the cause in a few lines and asks: "Retry the Chef?" If the operator agrees, the Waiter gives the Big Chef advice based on the traceback, and the Chef tries again. If the operator does not agree, the run continues without the tool. The retry needs the operator's approval each time; there is no automatic loop.
- **G2. Operator choice for each build (done, D56).** When a prompt leads to a new tool build, the operator chooses the spending cap and the DeepSeek effort (`low`, `high`, `max`) for that Chef run, before the Chef starts. The default values come from the configuration. `GATE=auto` uses the defaults without a question.
- **G3. Configurable Chef wall time (done, D55).** `CAP_SECONDS` (6 min) and `PLAN_SECONDS` (150 s) become settings, with an env variable and a Makefile variable. G2 can show the wall time with the cap and the effort.
- The DeepSeek effort must be configurable for each role. The env overrides exist now: `CHEF_<ROLE>_EFFORT`, `SHED_TOOL_EFFORT` and the Makefile `EFFORT` for the main agent. G2 adds the choice for each build (`options.effort`).

## 13. References
- `SUBJECT.md`: the hackathon brief and the hard rules.
- `PLAN.md`: the v7 plan and the demo script.
- `ARCHITECTURE.md`: the design and the decision log (D32–D63 are the v7 decisions).
- `OPEN_QUESTIONS.md`: open questions.
- `contract/INTERFACES.md`: the interfaces between all parts.
