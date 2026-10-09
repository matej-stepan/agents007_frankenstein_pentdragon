# Taltempla v7: interface contract

Status: prototype, 2026-10-08. This file fixes the interfaces between the parts of PLAN.md.
If you must change an interface, change this file in the same edit and tell the integrator.

## 0. Hard rules (from SUBJECT.md; code must keep them)
1. Generated code executes only in the toolshed container, as the user `tool`. The host never executes, imports or `exec`s tool code.
2. The DeepSeek key (`OAI_COMPATIBLE_KEY` in `.env`) lives only in the host process, in the meter. It never enters the container, an LLM context, a log, a trace, the DB or a tool. `make toolshed-up` never passes `.env` or the host env.
3. `register` needs a passed test run, an `approve` review and an operator approval, all for the same content hash.
4. Each gap comes from a task: `big_chef` needs a `lookup_id` from this session with `fit` = `none` or `partial`.
5. Caps are in code: the meter (USD), the Chef (tools, iterations, turns, time), the runner (time, memory, processes).
6. Never put a tool into the real registry (`/data/shed.db`) by hand or by a seed script. Test fixtures go only in a temp DB.

## 1. Layout
```
pyproject.toml uv.lock Makefile .containerignore      # uv workspace root (members cli, toolshed) - already made
cli/taltempla/   main.py loop.py llm.py meter.py ledger.sql prices.json prompt.md
                 shed_client.py gate.py approvals.py ws_tools.py commands.py ui.py
toolshed/        Containerfile tiers.py packages.json
toolshed/server/shed/   app.py db.py schema.sql manifest.py lookup.py runner.py chain.py llm.py pkgindex.py selftest.py
toolshed/server/shed/chef/  orchestrator.py static_check.py prompts/*.md
toolshed/sdk/shed_sdk/  main.py testing.py __init__.py (exports Shed)
contract/        INTERFACES.md manifest.schema.json
tests/           host pytest (no network, no container)
spikes/          spike_shed.sh
workspace/       in/ out/          (git-ignored; bind-mounted at /work)
.frank/          ledger.db permissions.json admin.token history run/meter.sock   (git-ignored; host state)
```
Python 3.13. The server and the SDK are **stdlib only**. The CLI uses `rich` and `prompt_toolkit` (3.0.53, it has `prompt_toolkit.shortcuts.choice`).

## 2. Runtime constants
| Name | Value |
|---|---|
| Image | `localhost/taltempla-shed:latest` (build context = repo root, `-f toolshed/Containerfile`) |
| Container | `taltempla-shed` |
| Data volume | `taltempla-data` → `/data` (dir 0700 root; `shed.db` inside) |
| Workspace | `./workspace` → `/work` (shedd makes `/work/out` and `/work/in`, mode 1777) |
| Meter socket dir | host `.frank/run/` (0700) → container `/run/meter` ; socket `meter.sock` (0600) |
| Admin API | shedd listens `0.0.0.0:7700` in the container; published `-p 127.0.0.1:7700:7700`. Host URL env `TALTEMPLA_SHED_URL` (default `http://127.0.0.1:7700`) |
| Admin token | host file `.frank/admin.token` (0600, made by `make toolshed-up` if missing) → container env `SHED_ADMIN_TOKEN`. shedd pops it from `os.environ` at start. |
| Runtime socket | `/run/shed/rt.sock` in the container, mode 0666 (the tool user connects; a run token authenticates) |
| Tool user | `tool`, uid/gid 1000, shell `/bin/bash`, no home in the image |
| Python in image | `/usr/local/bin/python3` (python:3.13-slim). Server at `/opt/shed/server`, SDK at `/opt/shed/sdk`, `PYTHONPATH=/opt/shed/server:/opt/shed/sdk`. The stdlib-only server and SDK run from source (no `uv sync` in the image). |
| Shared LLM client | canonical file `cli/taltempla/llm.py`; the Containerfile copies it to `/opt/shed/server/shed/llmclient.py`. With the dev mounts it is mounted at `/opt/shed/cli/taltempla/llm.py` and `shed/llm.py` imports it through its `taltempla.llm` fallback. |
| uv | `COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /uvx /usr/local/bin/` ; tiers install with `uv pip install --system` |

`toolshed-up` run flags (minimum): `podman run -d --name taltempla-shed -p 127.0.0.1:7700:7700 -v taltempla-data:/data -v $(PWD)/workspace:/work -v $(PWD)/.frank/run:/run/meter -e SHED_ADMIN_TOKEN=$$(cat .frank/admin.token) --pids-limit 2048 --memory 4g localhost/taltempla-shed:latest`. No `--env-file`, no `--env-host`, no home mount, no podman socket.

**Dev mounts (D51).** `toolshed-up` also adds `-v $(PWD)/toolshed/server:/opt/shed/server:ro -v $(PWD)/toolshed/sdk:/opt/shed/sdk:ro -v $(PWD)/cli/taltempla/llm.py:/opt/shed/cli/taltempla/llm.py:ro -e PYTHONPATH=/opt/shed/server:/opt/shed/sdk:/opt/shed/cli -e 'CHEF_*' -e 'SHED_TOOL_*'`. The host tree is the server code: `make toolshed-restart` (podman restart + health wait) loads new code without `make image`. Do not mount `llm.py` at `/opt/shed/server/shed/llmclient.py`: podman makes an empty file at that path in the host tree, and the host import breaks. The only host variables that go in are `CHEF_*` and `SHED_TOOL_*` (role model and effort overrides, never a key). The container has the label `taltempla.fp` = a hash of the run flags, the image ID and the `CHEF_*`/`SHED_TOOL_*` values. `toolshed-up` recreates the container when the label differs. The data volume stays. Recipes call sub-makes only through `$(SUBMAKE)`: with `.ONESHELL`, a recipe that names `$(MAKE)` executes in full under `make -n`.

Host env for the CLI (the Makefile sets them from its variables): `TALTEMPLA_MODEL` (default `deepseek-v4-pro`, Makefile `MODEL`), `TALTEMPLA_CAP_BUILD` (0.60), `TALTEMPLA_CAP_RUN` (2.00), `TALTEMPLA_CAP_SESSION` (5.00), `TALTEMPLA_CAP_TOTAL` (20.00), `TALTEMPLA_GATE` (`ask` | `auto`; `auto` = "Install + run once" and "Allow once" without a prompt, for headless smoke tests only), `TALTEMPLA_SHED_URL`, `TALTEMPLA_NO_SHED=1` (chat-only mode, no toolshed), `TALTEMPLA_EFFORT` (main agent `reasoning_effort`, default `high`, Makefile `EFFORT`), `TALTEMPLA_HOME` (project root; default: the nearest directory with `contract/` and `cli/`). The CLI reads `.env` itself (simple `KEY=VALUE` parser) for `OAI_COMPATIBLE_KEY` and optional `OAI_COMPATIBLE_BASE_URL` (default `https://api.deepseek.com`).

## 3. Shared LLM client: `cli/taltempla/llm.py` (stdlib only)
```python
class LLMError(Exception): status: int; body: dict | str
def post_json(body: dict, *, url: str | None = None, unix_socket: str | None = None, path: str = "/chat/completions",
              headers: dict | None = None, timeout: float = 600) -> dict
    # https URL or unix socket; retries 429/500/503 twice with backoff; raises LLMError on other non-2xx.
def assistant_message(resp: dict) -> dict
    # the message to append to history: role, content, reasoning_content (KEEP IT, DeepSeek 400s without it), tool_calls.
def tool_calls(resp: dict) -> list[tuple[str, str, dict | str]]   # (id, name, parsed args or raw string on JSON error)
def text(resp: dict) -> str
def tool_schema(name: str, description: str, parameters: dict) -> dict   # OpenAI "function" tool dict
```
DeepSeek body fields we use: `model`, `messages`, `tools`, `max_tokens`, `reasoning_effort` (`low|high|max`), `user_id`. Thinking is on by default; never send a forced `tool_choice`.
Verified response shape: `choices[0].message{content, reasoning_content, tool_calls[{id,type,function{name,arguments(JSON string)}}]}`, `finish_reason`, `usage{prompt_cache_hit_tokens, prompt_cache_miss_tokens, completion_tokens, completion_tokens_details.reasoning_tokens}`, `model`.

## 4. Meter: `cli/taltempla/meter.py` (host, in the CLI process; the only key holder)
```python
class MeterRefused(Exception): status = 402; info: dict   # {"cap": "build|run|session|total|tool_run", "limit": x, "spent": y, "need": z}
class MeterAuthError(Exception): status = 401
class Meter:
    def __init__(self, *, ledger_path: Path, prices_path: Path, key: str, base_url: str, caps: dict, session_id: str): ...
    def grant(self, kind: str, *, parent: str | None, cap_usd: float | None, label: str = "",
              run_id: str | None = None, build_id: str | None = None, tool: str | None = None) -> str
        # kinds: session, run, build, tool_run. Returns an opaque random token. A grant inherits run_id/build_id from its parent.
    def revoke(self, token: str) -> None
    def chat(self, grant: str, body: dict, *, role: str = "main", tool: str | None = None) -> dict
        # reserve -> forward -> settle. Adds resp["x_meter"] = {"cost_usd", "grant_spent_usd", "grant_left_usd"}.
    def spent(self, token: str) -> float
    def serve(self, sock_path: Path) -> None     # starts a daemon thread: HTTP over a unix socket (see below)
    def status(self) -> dict   # {"session_usd","run_usd","tokens","cache_pct","cap_left_usd"} for the status line
    def balance(self) -> float | None            # GET {base_url}/user/balance, USD total; None on error
```
- **Caps** (`caps` keys `build, run, session, total`): a grant's cap is `min(cap_usd, parent chain caps)`. The session grant is capped by `session`, and the ledger sum over all sessions is capped by `total`. A `run` grant is a child of the session grant; `build` and `tool_run` grants are children of the run grant.
- **Reserve before:** `est = (len(json.dumps(messages+tools))/3) * miss_price + max_tokens * out_price`. If `spent + outstanding + est > cap` at any level, raise `MeterRefused` (HTTP 402) and write a `refused` ledger row. Release the reserve after settle or error.
- **Clamp `max_tokens`** per role: `main 8192, plan 32768, tests 12288, code 32768, security 6144, tool 4096`; default when absent = the clamp value.

**Roles (D48).** Model, effort and clamp for each role. Every role has an env override.
| Role | Model / effort | max_tokens | Override (owner) |
|---|---|---|---|
| Main agent | `deepseek-v4-pro` / `high` | 8192 | `TALTEMPLA_MODEL`, `TALTEMPLA_EFFORT` (CLI) |
| P1 plan (also improve planning) | `deepseek-v4-pro` / `high` | 32768 | `CHEF_*` (Chef, `orchestrator.py`) |
| P2 tests | `deepseek-flash` / `low` | 12288 | `CHEF_*` |
| P3 code | `deepseek-flash` / `high`; `deepseek-v4-pro` / `high` from the first red test run or smoke failure | 32768 | `CHEF_*` |
| P4 security | `deepseek-flash` / `high` (env switch to pro) | 6144 | `CHEF_*` |
| `shed.llm` in tools | `deepseek-flash` / `low` | 4096 | `SHED_TOOL_MODEL`, `SHED_TOOL_EFFORT` (`chain.py`) |
The container gets `CHEF_*` and `SHED_TOOL_*` from the host only when `toolshed-up` makes it.
- **Settle after** from `usage`, at peak or off-peak price by the UTC request start time. Peak = 01:00–04:00 and 06:00–10:00 UTC, Monday–Friday. `prices.json`:
  `{"deepseek-flash": {"peak": {"hit": 0.006, "miss": 0.30, "out": 1.20}, "offpeak": {"hit": 0.003, "miss": 0.15, "out": 0.60}}, "deepseek-v4-pro": {"peak": {"hit": 0.044, "miss": 1.32, "out": 3.96}, "offpeak": {"hit": 0.022, "miss": 0.66, "out": 1.98}}}` (USD per 1M tokens). Unknown model → flash prices.
- **Ledger** `.frank/ledger.db` (`ledger.sql`): table `calls(id, ts, session_id, run_id, build_id, grant_kind, role, tool, model, hit, miss, out, reasoning, cost_usd, status ok|error|refused, latency_ms)`, table `sessions(id, started, balance_start, balance_end)`, table `savings(session_id, tool, version, saved_usd, ts)`.
- **Unix socket server** (`.frank/run/meter.sock`, dir 0700, socket 0600, unlink a stale file first): `POST /v1/chat/completions`, headers `Authorization: Bearer <grant>`, `X-Taltempla-Role: plan|tests|code|security|tool`, optional `X-Taltempla-Tool: <name>`. Body = OpenAI chat body. Reply = upstream JSON + `x_meter`. Errors: 401 `{"error":{"type":"bad_grant"}}`, 402 `{"error":{"type":"cap_exceeded", ...info}}`, 502 `{"error":{"type":"upstream","status":n,"body":...}}`. Use `socketserver.ThreadingMixIn` + `UnixStreamServer` + `BaseHTTPRequestHandler`.

## 5. Admin API (shedd `app.py`, HTTP JSON, `ThreadingHTTPServer`)
All routes except `GET /health` need `Authorization: Bearer <admin token>` (else 401). Errors: `{"error": "message"}` with 4xx/5xx.
| Route | Request | Reply |
|---|---|---|
| `GET /health` | — | `{"ok":true,"tools":n,"meter":bool}` (`meter` = `/run/meter/meter.sock` exists) |
| `GET /tools` | — | `{"tools":[{name, grade, version, summary, uses, permissions, content_hash, perm_hash, origin, build_cost_usd, invocations, failures, created_at}]}` (active versions) |
| `POST /lookup` | `{"query": str, "session_id": str}` | `{"lookup_id": str, "fit": "good|partial|none", "rows": [{name, grade, version, summary, uses, score, fit, uncovered}]}` (≤3 rows, big tools first; `uncovered` = the query terms the row does not cover) |
| `POST /lookup` | `{"tool": name}` | `{name, version, grade, skill, input_schema, output_schema, uses, permissions}` |
| `POST /invoke` | `{"name", "args", "grant", "run_id", "session_id"}` | `{"ok": bool, "invoke_id", "name", "version", "result"?, "error"?, "preview"?, "out_path"?, "duration_ms", "subcalls": [{name, version, ok, duration_ms}], "refused_subcalls"? (count of subcalls the depth/count/deadline limits refused), "llm_cost_usd"}` |
| `POST /chef/build` | `{"task", "need", "lookup_id", "session_id", "build_id", "grant", "repair_of"?: {"tool", "invoke_id", "problem"?, "args"?}}` | NDJSON stream (§8) |
| `GET /drafts/{content_hash}` | — | `{manifest, files: {"tool.py","test_tool.py","SKILL.md"}, test_runs: [...], review}` |
| `POST /register` | `{"build_id", "content_hashes": [...], "approval": {"mode": "once|always", "by": "operator"}}` | `{"registered": [{name, version, content_hash, perm_hash}]}`; 409 if a hash lacks a passed test run or an `approve` review |
| `POST /reject` | `{"build_id"}` | `{"ok": true}` |
| `POST /rollback` | `{"name", "version"?}` (default: the previous version) | `{"name", "active_version"}` |
| `GET /history?name=X` | — | `{"versions": [...], "events": [...]}` |
| `GET /stats` | — | `{"tools": [{name, version, grade, invocations, failures, avg_ms, build_cost_usd, llm_cost_usd}]}` (active versions; the list is what `shed.registry()` returns) |

- **Invoke result size:** if `json.dumps(result)` > 6144 bytes, shedd writes it to `/work/out/<name>-<invoke_id>.json`, sets `out_path` (host path `workspace/out/...`) and `preview` (first ~1500 chars), and omits `result`.
- `invoke` records an `invoked` event `{invoke_id, ok, duration_ms, error?, run_id, session_id, ...}` (no args stored; args can hold personal data). `db.get_invocation` returns `{invoke_id, tool, version, ok, error, session_id}`.
- **Lookup match:** a query term matches as a prefix only in name, summary and keywords; it matches as a whole (stemmed) term in every column. Thus `car` does not match `cards` in a description.
- `big_chef` gap rule: `/chef/build` refuses (400) unless `lookup_id` exists, has the same `session_id`, and `fit` is `none` or `partial`; or `repair_of` names an invocation of `repair_of.tool` and one of these is true:
  - **repair:** `ok == false` and the error does not start with `ValueError` (an argument error: the agent fixes the args);
  - **improve (D50):** `ok == true`, `problem` is not empty, and the invocation `session_id` equals the request `session_id`.
- The CLI attaches `repair_of.args` (the args it recorded for that `invoke_id` in this session, or null); the model never supplies them. shedd adds `error` (the invocation error or null) and gives the Chef `BuildCtx.repair_of = {tool, invoke_id, problem, args, error}`. The args go into the build record and into the new regression test.

## 6. Tool package and SDK
A version = `manifest.json` (see `contract/manifest.schema.json`) + `tool.py` + `test_tool.py` + `SKILL.md` (≤1200 chars).
- `tool.py` defines `def run(args: dict, shed) -> dict`. Big tools return `{"results": [...], "warnings": [...], "sources": [...]}`.
- **Content hash** = sha256 over canonical JSON of `{"manifest": manifest minus ["version","parent"], "files": {name: text}}` (`json.dumps(sort_keys=True, separators=(",",":"))`).
- **Permission hash** = sha256 over canonical JSON of `{name, uses, deps, permissions}`.
- Both in `shed/manifest.py`: `content_hash(manifest, files)`, `perm_hash(manifest)`, `validate(manifest) -> list[str]`.

SDK (`shed_sdk`, stdlib only), used by tool code:
```python
class Shed:                                  # real one, talks to /run/shed/rt.sock with SHED_RUN_TOKEN
    def call(self, name: str, args: dict) -> dict          # only names in manifest "uses"; raises ShedError
    def llm(self, prompt: str | list[dict], *, max_tokens: int = 1024, json: bool = False) -> str
    def registry(self) -> list[dict]                       # read-only /stats rows
    work: Path = Path("/work"); out_dir: Path = Path("/work/out")
    def log(self, msg: str) -> None                        # stderr
class ShedError(Exception): ...
```
`python3 -m shed_sdk.main <tool_dir> <args.json> <result.json>`: imports `tool.py` from `tool_dir`, calls `run(args, Shed())`, writes `{"ok": true, "result": ...}` or `{"ok": false, "error": "...", "traceback": "..."}` to `result.json`.

Testing (`shed_sdk.testing`):
```python
class MockShed(Shed):
    def mock(self, name: str, value_or_fn) -> None          # shed.call(name, args) returns value or fn(args)
    def mock_llm(self, value_or_fn) -> None                 # shed.llm(...) returns str or fn(prompt)
    calls: list[tuple[str, dict]]; llm_prompts: list
live = pytest.mark.live   # at most one live smoke test per tool (it may use the network)
```
Tests do `from tool import run` and `from shed_sdk.testing import MockShed`.

## 7. Runtime socket (`/run/shed/rt.sock`, `chain.py`)
One JSON line request, one JSON line reply, per connection. Every request has `"token": SHED_RUN_TOKEN`.
| op | Request fields | Reply |
|---|---|---|
| `call` | `name, args` | `{"ok":true,"result":...}` or `{"ok":false,"error":...}` |
| `llm` | `messages, max_tokens, json` | `{"ok":true,"content":str,"cost_usd":x}` |
| `registry` | — | `{"ok":true,"tools":[...]}` (the `/stats` rows) |
A run token maps to `{tool, version, uses, depth, root_invoke_id, grant, deadline}`. Limits per root invocation: depth ≤ 3, subcalls ≤ 20, the root deadline. `call` refuses a name outside the caller's `uses`. `llm` goes to the meter with the root grant (role `tool`, `X-Taltempla-Tool`); the CLI caps that `tool_run` grant at the sum of `llm_usd` over the entry tool and every tool it reaches through `uses`, and shedd holds each tool to its own `llm_usd` within one root run; a tool with `permissions.llm_usd == 0` gets a refusal. The token dies when its process exits.

**Runner** (`runner.py`), every execution of agent code:
`setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs -- prlimit --as=<limit> --nproc=512 --nofile=1024 -- timeout -k 2 <t> env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=<run tmp> LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/shed/sdk:<tool dir> SHED_SOCK=/run/shed/rt.sock SHED_RUN_TOKEN=<tok> python3 ...`.
Defaults: timeout 60 s (manifest `limits.timeout_s`, max 180), address space 2 GiB (`limits.memory_mb`, max 4096). Each run gets a fresh dir under `/tmp/shed-run/<id>` (files owned by root 0644, dir 0755) and a writable `HOME` owned by `tool`.
```python
@dataclass class ExecResult: exit: int; stdout: str; stderr: str; timed_out: bool; duration_ms: int
@dataclass class TestResult: passed: int; failed: int; errors: int; log: str; duration_ms: int; timed_out: bool
def probe(code: str, files: dict[str, str] | None = None, timeout_s: int = 30) -> ExecResult
def run_tests(files: dict[str, str], timeout_s: int = 120) -> TestResult     # pytest -q -p no:cacheprovider test_tool.py
def run_tool(manifest: dict, files: dict[str, str], args: dict, token: str, timeout_s: int) -> dict  # the result.json content
```

## 8. Big Chef (`chef/orchestrator.py`, `chef/static_check.py`)
```python
@dataclass class BuildCtx: db; grant: str; task: str; need: str; lookup_id: str | None; session_id: str; build_id: str; repair_of: dict | None
    # repair_of = {tool, invoke_id, problem: str ("" = repair a failure), args: dict | None, error: str | None}
def build(ctx: BuildCtx, emit: Callable[[dict], None]) -> dict        # returns the handoff dict (also emitted)
def check(code: str, manifest: dict) -> list[dict]                    # static_check: [{"rule","line","msg"}]; empty = pass
```
LLM calls: `shed.llm.chat(grant, body, role, tool=None) -> dict` (meter over the socket; raises `shed.llm.CapExceeded` on 402).
Caps per build: 3 new small tools (+ the entry tool), 4+1 coder iterations per tool, 40 LLM turns, 12 min wall time; USD by the meter (`build` grant, $0.60). A 402 stops the build with `failed`.

NDJSON events (one JSON object per line, flush each line):
- `{"type":"trace","phase":"P1|P2|P3|P4|P5","tool":str|null,"msg":str,"cost_usd":float}` — one dim line per Chef step (cumulative cost).
- `{"type":"test_run","tool","kind":"stub_sanity|tests","passed","failed","iteration","excerpt"}`
- `{"type":"handoff","build_id","entry":name,"tree":[{name, grade, status:"new"|"reused", version?, content_hash?, perm_hash?, uses, deps, permissions, tests:"n/n", iterations, verdict, cost_usd, summary}],"cost_usd","skill":"<entry SKILL.md>","warnings":[]}`
- `{"type":"failed","reason":str,"cost_usd":float}`
Order of `tree`: new small tools first, then the entry tool. Every new tool in the tree has a passed test run and an `approve` review in the DB before the handoff.

## 9. Agent toolset (CLI, fixed; schemas never change in a session)
| Tool | Params | Result to the model (compact JSON) |
|---|---|---|
| `lookup` | `query?: str`, `tool?: str` | the `/lookup` reply |
| `use_tool` | `name: str`, `args: object` | the `/invoke` reply (preview + path when large). An ok reply gets `"problems": [...]` second (after `ok`) when `smoke_problems` finds any |
| `big_chef` | `task: str`, `need: str`, `lookup_id: str`, `repair_of?: {tool, invoke_id, problem?}` | `{"installed": [...], "entry", "skill", "cost_usd"}` or `{"rejected": true}` / `{"failed": reason}` |
| `ws_read` | `path: str` | text (≤64 KB) of `workspace/<path>` |
| `ws_write` | `path: str`, `content: str` | `{"ok": true, "path"}`; jailed to `workspace/` |
The use gate runs in the CLI before each `use_tool`. The CLI refuses `big_chef` (no repair) when a lookup newer than its `lookup_id` in this session returned `fit=good` (stale gap evidence), and after 2 builds per prompt. A build counts only after shedd streams its first event: a refused request (400) does not use the budget.
`smoke_problems(result)` (CLI, `loop.py`) checks an ok result (or the `out_path` JSON of a large result): the result is empty; `results` is empty; a field is null in all rows of a list of 2 or more dict rows; `warnings` is not empty. The CLI keeps `invoke_id -> args` for each `use_tool` call of the session. "Always allow" is stored in `.frank/permissions.json` keyed by the permission hash.

## 10. shedd internals used by the Chef (owner: shedd; user: chef)
```python
# shed/db.py   (sqlite3, one connection per thread or a lock; WAL; /data/shed.db)
class DB:
    def __init__(self, path: str): ...
    def active_tools(self) -> list[dict]          # active versions: name, grade, version, summary, description, keywords, uses,
                                                  #   deps, permissions, input_schema, output_schema, content_hash, perm_hash, build_cost_usd
    def get_version(self, name: str, version: int | None = None) -> dict | None
                                                  # + "manifest", "files" {"tool.py","test_tool.py","SKILL.md"}, "parent"
    def save_draft(self, build_id: str, manifest: dict, files: dict[str, str], cost_usd: float = 0.0) -> dict   # {"content_hash","perm_hash"}
    def get_draft(self, content_hash: str) -> dict | None
    def record_test_run(self, content_hash: str, kind: str, passed: int, failed: int, log: str) -> str   # kind tests|stub_sanity
    def record_review(self, content_hash: str, verdict: str, report: str) -> None    # verdict approve|reject
    def record_event(self, kind: str, tool: str | None, version: int | None, data: dict) -> None
    def start_build(self, build_id, session_id, task, need, lookup_id, repair_of) -> None
    def add_trace(self, build_id: str, event: dict) -> None
    def finish_build(self, build_id: str, status: str, cost_usd: float, handoff: dict | None) -> None   # status handoff|failed|registered|rejected
    def get_lookup(self, lookup_id: str) -> dict | None      # {id, session_id, query, fit, rows}
    def get_invocation(self, invoke_id: str) -> dict | None  # {invoke_id, tool, version, ok, error}
    def register(self, content_hash: str, approval: dict) -> dict   # checks rule 3; new version n+1; active pointer; FTS row; event
    def rollback(self, name: str, version: int | None) -> int
# Append-only: BEFORE UPDATE/DELETE triggers RAISE(ABORT) on versions, test_runs, reviews, approvals, events, builds_log.
# Mutable: active(name, version), drafts, builds (status), lookups, packages.

# shed/lookup.py
def lookup(db, query: str, session_id: str) -> dict          # §5 reply; stores the lookup row
def explore(db, query: str, page: int = 0, k: int = 5) -> list[dict]   # name, grade, version, summary, uses, score

# shed/pkgindex.py   (catalog = /opt/shed/packages.json in the image, toolshed/packages.json on the host)
def search(query: str, k: int = 8) -> list[dict]             # {name, category, installed}
def in_catalog(name: str) -> bool
def is_installed(name: str) -> bool
def install(name: str, timeout_s: int = 300) -> tuple[bool, str]   # uv pip install --system; catalog names only; records the package

# shed/llm.py
class CapExceeded(Exception): info: dict
def chat(grant: str, body: dict, role: str, tool: str | None = None) -> dict   # meter over /run/meter/meter.sock
```
