# TIGRIS

**A self-extending agent that builds its own tools, and runs every line of generated code in a sandbox.**

TIGRIS starts with no domain tools. When a task needs a capability it does not have, it detects the gap. Its Big Chef then plans the tool against real data, writes the tests first, codes until they pass, reviews the code, and asks you to approve the install. The tool is versioned in a persistent registry, used at once, and reused or extended by later sessions.

> Capabilities grow; authority does not.

Built for hackathon Case 03 ("Frankenstein": *create → test → install → evolve*). The code and the CLI command still use the working name `taltempla`.

---

## Dependencies

| Need | Version / note |
|---|---|
| Linux host | Tested on Debian 13 |
| [Podman](https://podman.io/), **rootless**, with `passt` and `uidmap` | `sudo apt install -y podman passt uidmap` |
| A `/etc/subuid` + `/etc/subgid` entry for your user | `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER` |
| [uv](https://docs.astral.sh/uv/) | Installs Python 3.13 and all Python packages |
| An API key for an **OpenAI-compatible** endpoint | DeepSeek by default (see [BYOK](#bring-your-own-key-byok)) |
| `curl`, `make`, `bash` | For the Makefile |

Python packages (installed by `uv sync`): `rich` and `prompt_toolkit` for the CLI, and `pytest` and `ruff` for development. The toolshed image is `python:3.13-slim` with a pre-installed tier of scraping and data packages (`toolshed/packages.json`, `toolshed/tiers.py`).

## Bring your own key (BYOK)

TIGRIS calls **any OpenAI-compatible chat-completions API** with your own key. The default is the [DeepSeek API](https://api-docs.deepseek.com/) with `deepseek-v4-pro` (thinking mode, prompt caching).

Make a `.env` file in the repository root. It is in `.gitignore`; never commit it:

```sh
OAI_COMPATIBLE_KEY=sk-...
# optional; the default is https://api.deepseek.com
OAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
```

- **Only the host meter reads the key.** It never enters the sandbox container, a tool, or a prompt.
- To use another provider, set `OAI_COMPATIBLE_BASE_URL` and `MODEL=...`, and add the model's prices (USD per 1M tokens) to `cli/taltempla/prices.json`. Without an entry, the meter uses the `deepseek-v4-pro` prices for its caps. Only DeepSeek is tested.
- **Data notice:** prompts, tool code and tool output go to the API you configure. The DeepSeek API stores data in the PRC and can use it for training. Do not enter personal data.

## Install

```sh
make deps          # prints the apt line, installs uv if missing, runs uv sync
make doctor        # 6 preflight checks; each failure prints its fix
make toolshed-up   # builds the sandbox image if missing and starts the toolshed container
```

## Usage

```sh
make run           # start the interactive CLI (runs doctor, starts the toolshed if it is down)
```

Ask for something TIGRIS cannot do yet, for example *"find the cheapest house for sale near Brno"*. The run then goes like this:

1. **lookup** searches the registry (bm25 + coverage, no LLM): fit `good`, `partial` or `none`.
2. If no tool fits, you choose the **build options**: USD cap, reasoning effort and wall time (or cancel).
3. The **Big Chef** builds the tool. Its progress, test runs and cost stream to the terminal.
4. The **install gate** shows the tool tree, permissions (network, LLM budget, files), tests `n/n` and the security verdict. You choose install, always allow, or reject.
5. The agent runs the tool and answers. Later sessions find the tool and reuse it.

### Commands in the CLI

| Command | Effect |
|---|---|
| `/shed` | List the registry (tools, versions, grades) |
| `/cost` | Spend by run, role and tool, cache hits, and savings from reuse |
| `/history NAME` | Versions and events of a tool |
| `/rollback NAME [V]` | Make an older version active |
| `/allow` / `/allow revoke NAME\|HASH` | List / revoke "always allow" approvals |
| `/help` | Help |
| `/exit`, `exit`, Ctrl-D | Quit (Ctrl-C on an empty line also quits) |

### Make targets

| Target | Effect |
|---|---|
| `make run` | Interactive CLI |
| `make shed` / `make cost` | Print the registry / the ledger |
| `make history T=name` / `make rollback T=name [V=n]` | Tool history / rollback |
| `make toolshed-up` / `-down` / `-restart` / `-logs` / `-shell` | Manage the sandbox container |
| `make demo-reset` | Back up, then clear the registry, ledger and outputs (a fresh start) |
| `make toolshed-reset` | Delete the container and **all** registry data |
| `make test` / `make lint` | Offline tests (+ in-container selftest) / ruff |
| `make help` | List every target |

Headless, for scripts and tests (auto-approves every gate, so use it only for testing):

```sh
TALTEMPLA_GATE=auto TALTEMPLA_CAP_RUN=0.4 uv run taltempla --once "your prompt"
uv run taltempla shed | cost | history NAME | rollback NAME [V] | allow
```

### Configuration

Override on the command line, for example `make run CAP_RUN=1 BUILD_SECONDS=600`.

| Variable | Default | Meaning |
|---|---|---|
| `MODEL` / `EFFORT` | `deepseek-v4-pro` / `high` | Main agent model and reasoning effort |
| `CAP_BUILD` / `CAP_RUN` / `CAP_SESSION` / `CAP_TOTAL` | 1.00 / 3.00 / 6.00 / 20.00 USD | Spend caps, enforced by the meter |
| `BUILD_EFFORT` | empty (each Chef role uses its own) | Default effort for a build: `low`, `high` or `max` |
| `BUILD_SECONDS` / `PLAN_SECONDS` | 360 / 150 | Default wall time for a build / for its planning phase |
| `GATE` | `ask` | `auto` approves every gate (tests only) |
| `CHEF_<ROLE>_MODEL`, `CHEF_<ROLE>_EFFORT` | `deepseek-v4-pro`, `high` | Per-role overrides (`plan`, `tests`, `code`, `security`, `escalate`) |
| `SHED_TOOL_MODEL`, `SHED_TOOL_EFFORT` | | Model and effort for tools that call an LLM |

---

## Sandboxed execution

Generated code **never runs on the host**. TIGRIS has two modules:

```
 HOST                                           ROOTLESS PODMAN CONTAINER ("toolshed")
 ┌───────────────────────────────┐              ┌─────────────────────────────────────────┐
 │ CLI agent loop (5 fixed tools)│──admin API──▶│ registry (SQLite, versions, FTS5)       │
 │ install / use gates           │ 127.0.0.1    │ Big Chef: plan → tests → code → review  │
 │ METER: the only key holder,   │◀─unix socket─│ tool runner: uid 1000, env -i, prlimit, │
 │ caps, ledger                  │  (grants)    │ timeout; no key, no host env            │
 └──────────────┬────────────────┘              └─────────────────────────────────────────┘
                ▼ OpenAI-compatible API (your key)
```

- **Rootless Podman.** The whole toolshed runs as one rootless container. Its admin API is published on `127.0.0.1` only.
- **Unprivileged execution.** Each tool run uses `setpriv` (uid 1000), `env -i` (empty environment), `prlimit` (2 GiB address space, 512 processes) and `timeout` (60 s by default, 180 s max).
- **No credentials inside.** The container gets neither the API key, `.env`, nor the host environment. Tools that need an LLM call it through `shed.llm`, which goes over a unix socket to the host meter with a short-lived, capped **grant**.
- **Isolation inside the container.** The tool user cannot read the registry database, the meter socket or `/proc/1/environ`. A tool can call only the tools it declares in `uses`, with call depth ≤ 3 and a capped number of subcalls. A selftest checks all of this (`make test`).
- **Packages from a catalog only.** The Chef can install only packages listed in `toolshed/packages.json`.
- **Exchange directory.** Tools write results to `workspace/out/`. Results larger than 6 KB go to a file, not into the agent's context.

## How a tool is built

| Phase | What happens |
|---|---|
| **P1 Plan** | Probes the real sources (live samples), then decides what to reuse, extend or create. The main tool is always a task-level "big" tool. |
| **P2 Tests** | Writes `test_tool.py` from the spec and the samples, never from the code. The tests must fail on a stub. |
| **P3 Code** | Writes the tool, then edits it until the tests pass (capped iterations). New tools build in parallel. A live smoke run checks the main tool on real data. |
| **P4 Security** | Static AST checks, then an LLM review. A reject costs one more coder iteration. |
| **P5 Handoff** | The install gate: tool tree, permissions, tests, iterations, verdict and cost. |

- **Big and small tools.** Small tools are building blocks that only big tools call, inside the sandbox. The agent itself can run only big tools, so it cannot fake work by calling a fetcher with invented URLs.
- **Evolve.** When a tool runs but its result has problems (empty results, null fields, warnings), the agent starts *improve* mode. The Chef keeps the old tests, adds new ones, and registers version n+1. `/rollback` returns to any version.
- **Failure handling.** If a build fails, the **Waiter** explains the cause in a few lines. It then offers a retry with advice, or a resume from the checkpoint (the saved plan and the tools that already pass). Each retry needs your approval.

## Cost control

- **The meter controls every LLM call** (agent, Chef and tools). It reserves before a call and settles after it, writes each call to an append-only ledger (`.frank/ledger.db`), and enforces the USD caps for build, run, session and total.
- **Limits in code:** 40 agent turns and 2 builds per prompt; for each build, 48 LLM turns, 3 new small tools and 4+1 coder iterations per tool, plus the build's wall time.
- **When a build hits its cap,** it pauses and asks you to raise the cap or the time, instead of throwing its work away.
- **Reuse is measured.** `/cost` shows the build cost that reuse saved.

## Repository layout

```
cli/taltempla/        host: agent loop, meter, gates, Waiter, shed client, prompt
toolshed/server/shed/ container: admin API, registry DB, lookup, runner, chain, Big Chef (chef/)
toolshed/sdk/         shed_sdk for tool code (Shed, MockShed for tests)
toolshed/Containerfile, tiers.py, packages.json   the sandbox image
contract/             INTERFACES.md and manifest.schema.json (binding interfaces)
tests/                offline tests (about 3 s)
spikes/               container and metered-chain spikes
```

## Limitations

- Builds are slow: planning takes minutes with `deepseek-v4-pro` at high effort. Builds can also fail; then the Waiter takes over.
- Tool lookup can rate a general tool as a good fit for a more specific task.
- Tools that scrape websites break when sites block bots or change their pages. Improve mode repairs them.
- There is no explicit handling of the search language versus the answer language yet.
- Linux with rootless Podman only. Only DeepSeek is tested as the provider.

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): the design and the decision log
- [contract/INTERFACES.md](contract/INTERFACES.md): the interfaces between the parts
- [HANDOFF.md](HANDOFF.md): the current state and open items
- [PLAN.md](PLAN.md), [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md), [SUBJECT.md](SUBJECT.md) (the hackathon brief)

## License

GPL-3.0. See [LICENSE](LICENSE).
