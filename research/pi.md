<!-- Raw research notes from the planning workflow, 2026-10-08. Not ASD-STE100. Facts can change: verify before use. -->

Pi brief for the CLI module. Source: `earendil-works/pi` @6fb2e78 (2026-10-08), `@earendil-works/pi-coding-agent` **1.1.0** (ARCHITECTURE.md §10 says Pi 1.0, so it needs an update). Paths are relative to `packages/coding-agent/` unless marked `ai/` (`packages/ai/`). Clone (read only): `/tmp/claude-1000/-home-truecat-dny-10-hackathon01/2c6e364e-721c-4687-ade4-a15fe4468b02/scratchpad/pi-src`

## 1. Pi package or our own binary

- **Pi package:** a directory with `pi.extensions/skills/prompts/themes` in `package.json`. Load it with `pi -e ./client` or `pi install ./client` (docs/packages.md). It is simple and uses the whole stock TUI. The weakness: enforcement depends on how someone launches it. A plain `pi` also loads the user's global extensions and settings.
- **Own binary with the SDK:** `createAgentSession()` plus `InteractiveMode` (src/index.ts:416). SDK sessions do not load the built-in MCP, tool_search or codemode extensions, so we must wire them ourselves (docs/sdk.md "codemode-mcp"). We would also re-implement argument parsing and session selection. This costs too much for a hackathon.
- **Middle option:** `main(args, { extensionFactories })` is exported (src/main.ts:569-577, src/index.ts:413). A 5-line binary can run the full Pi CLI with our extensions. It is **not documented** (UNVERIFIED as stable).
- **Recommended:** keep the client as a local Pi package. Start it only through `make run` (a thin launcher) with fixed, documented flags:
  - `PI_CODING_AGENT_DIR=./.frank/agent` (separate agent directory; docs/configuration.md)
  - `-ne -e builtin:mcp -e builtin:tool-search -e ./client` (cli.md:194-197)
  - `-nc` (stops `AGENTS.md`/`CLAUDE.md` from our repo entering the agent's context, cli.md:214)
  - `--models 'deepseek/*'`
  - `--exclude-tools bash,powershell,codemode`
- Add a fail-closed guard. On `session_start`, the extension checks `pi.getAllTools()`. If `bash` or `codemode` is present, it calls `ctx.shutdown()`. Then even a wrong launch cannot run code on the host.
- For the reskin later: themes (docs/themes.md, `--theme`), plus the custom-header, custom-footer and editor examples. The app name comes from `piConfig.name` in Pi's own `package.json` (src/config.ts:577-586; it can be redirected with `PI_PACKAGE_DIR`). UNVERIFIED for our use.

## 2. Removing built-in tools

- Available controls: `defaultTools` setting, `--tools`, `-nbt` (`--no-builtin-tools`), `-nt` (`--no-tools`), `-xt` (`--exclude-tools`) (cli.md:119-128, settings.md "Tools"). SDK: `tools`, `noTools`, `excludeTools` (src/core/sdk.ts:71-92).
- **Only `--exclude-tools` / `excludeTools` is strong.** `_isAllowedTool()` removes the excluded name from the registry. This applies to the built-in tool **and** to any extension tool with the same name (src/core/agent-session.ts:1537-1541, 3505-3514). After that, neither `pi.setActiveTools()` nor `pi.registerTool("bash")` can bring it back.
- If we only change `defaultTools` (`-bash`), bash stays registered. Any extension can then turn it on again with `pi.setActiveTools()`.
- The model cannot change the tool set itself. `tool_search` loads only `deferred`/`codemode` tools. An inactive `direct` tool cannot be called, not even from codemode (extensions.md "Tool exposure").
- Two more host paths remain open:
  - The operator's `!cmd` input. Block it with a `user_bash` handler that returns `{result}` (src/core/extensions/types.ts:1438).
  - Extensions themselves can run host commands with `pi.exec()`. This is our trusted code.

## 3. Enforcement hooks (src/core/extensions/types.ts)

| Hook | What it can do |
|---|---|
| `input` (:1144) | Return `continue`, `transform{text}` or `handled`. `handled` refuses a run (for example, the governor has no budget left). |
| `before_agent_start` (:920) | Gets the prompt and changeable `systemPromptOptions`. Returns `{message, systemPrompt}` (:1468). It is awaited, and the returned message is added as a `custom` message after the user message (agent-session.ts:2062-2102). It can also call `pi.setActiveTools()`. **This is where the gap pass goes.** |
| `tool_call` (:1166) | Change the input in place, or return `{block, reason, terminate}` (:1426). A handler that throws blocks the call (fail-safe). It also runs for nested and MCP calls. |
| `tool_result` (:1240) | Replace `content`, `details`, `structuredContent`, `isError`, `usage` (:1455). |
| `turn_end` / `agent_before_settle` (:1038/:1001) | Add `custom`, `custom_message`, `context_edit` or `compaction` entries, and request one `continue` (:994). |
| `agent_settled` | Notification only. |
| `message_end` | Gives the final assistant message with `usage.cost`. Use it for the ledger. |
| `ctx.abort()` | Use it for the turn and USD caps. |

- `before_provider_request` is **not** a gate: errors in it are swallowed (src/core/extensions/runner.ts:1361-1390). For a check before each call, use `ctx.abort()` in `turn_start`. It is UNVERIFIED that this cancels the pending request.
- **Nested LLM calls:** `ctx.modelRegistry.complete()` / `streamSimple()` / `stream()` (src/core/model-registry.ts:128-147). They return an `AssistantMessage` with `usage.cost`.
- **Where nested usage is counted:**
  - Inside a tool, return the `usage` in the result. It then counts in session totals (extensions.md "Tools"; agent-session.ts:4183-4213).
  - In event handlers there is **no API to record usage**: `ReadonlySessionManager` has no `appendUsage` (src/core/session-manager.ts:245). So the gap-pass cost will not appear in Pi's footer. The governor ledger must be the authority, and it can persist with `pi.appendEntry()`.

## 4. MCP

- Configuration: `~/.pi/agent/mcp.json`, or `.pi/mcp.json` (only after project trust). From code: `pi.registerMcpServer(name, {url|command, exposure, toolExposure, timeout})`, which is not saved to disk (extensions.md "MCP servers"). Recommended: register the toolshed from our extension.
- **Runtime tool changes:** Pi handles `notifications/tools/list_changed`. New tools are added and withdrawn tools become unreachable (src/extensions/mcp/runtime.ts:399-404; mcp.md). `mcp_servers_change` is a different event: it fires when an extension registers or unregisters a server (:731).
- **Exposure modes:** `direct`, `deferred` (found with `tool_search`), `codemode` (the default), `hidden`. `toolExposure` can set the mode per tool (mcp.md "Control tool exposure").
- The first prompt waits up to 10 s only for `direct` servers.
- Changing the tool set in the middle of a conversation sends a full transcript checkpoint to DeepSeek (`supportsMidConvoToolAdditions:false`). The prompt cache is then lost.
- **Codemode runs on the host:** a QuickJS VM compiled to WASM, in a Node `worker_thread` (codemode/README.md; codemode/src/runtime/worker.ts). It has no file system and no network. Its only power is calling tools through the `tool_call` pipeline. Model-written JavaScript still runs on the host, so exclude it (this answers the codemode question in OPEN_QUESTIONS.md).

## 5. Usage and cost

- `Usage` has `input`, `output`, `cacheRead`, `cacheWrite`, `reasoning`, `totalTokens` and `cost{input, output, cacheRead, cacheWrite, total}` in USD (ai/src/types.ts:446).
- The cost is computed with `calculateCost()` from `model.cost` (USD per million tokens, with optional tiers) (ai/src/models.ts:1200).
- For DeepSeek, cache hits are read from `prompt_cache_hit_tokens` (ai/src/api/openai-completions.ts:1530-1558).
- **Custom prices:** `models.json` → `providers.<p>.modelOverrides.<id>.cost`, or `cost` on a custom model (schemas/models.schema.json:179, 207-226).
- The price catalog can be updated from pi.dev at runtime (models.md). To keep the ledger stable, pin prices with `modelOverrides` and/or use `--offline`.

## 6. DeepSeek

- **DeepSeek is built in.** Provider `deepseek`, base URL `https://api.deepseek.com`, API `openai-completions`, key in `DEEPSEEK_API_KEY` (ai/src/providers/deepseek.ts; docs/providers.md:36).
- Pi models (ai/scripts/generate-models.ts:2980-3025; pi.dev/models):

  | Model | Price in/out/cache-read (USD per million) | Thinking levels | Input |
  |---|---|---|---|
  | `deepseek-flash` (V4.1 Flash) | 0.3 / 1.2 / 0.006 | off, low, high, max | text, image |
  | `deepseek-v4-pro` | 1.32 / 3.96 / 0.044 | off, high, max | text |

  Both have a 1M-token context window.
- The DeepSeek pricing page confirms these model IDs. Pi's prices are **peak** rates. Off-peak is half, and Pi cannot represent that (noted in the generator). So Pi's numbers are an upper bound, which is safe for caps.
- Behaviour Pi already handles (openai-completions.ts:928-937, 1611-1660):
  - sends `thinking:{type}` plus `reasoning_effort`
  - passes `reasoning_content` back on assistant messages
  - uses `max_tokens`
  - sends no developer role and no `store`
- **The OpenAI SDK is not necessary.** `ctx.modelRegistry` covers nested calls. Set the thinking level explicitly, for example `deepseek/deepseek-flash:off` for the gap pass. The default `medium` does not exist on these models and gets clamped.

## 7. Sessions, config, trust, themes, sub-agents

- **Sessions:** JSONL files under `~/.pi/agent/sessions/`, grouped by working directory. Override with `--session-dir`, `PI_CODING_AGENT_SESSION_DIR` or the `sessionDir` setting (sessions.md, session-format.md). A plain `pi` (without `-c`) starts a fresh session, which is what session 2 of the demo needs.
- **Project config:** `.pi/{settings.json, mcp.json, extensions/, skills/, prompts/, themes/, SYSTEM.md, APPEND_SYSTEM.md}`. All of it is trust-gated. Trust comes from `--approve`, `/trust`, `~/.pi/agent/trust.json`, `defaultProjectTrust`, or a `project_trust` handler in an `-e` or personal extension (security.md). Recommendation: put no `.pi/` folder in the repo; put the configuration in the launcher instead.
- **Themes:** JSON files (schemas/theme.schema.json), loaded with `--theme` / `--use-theme`.
- **Sub-agents:** the subagent example starts a separate `pi --mode json -p --no-session` process, with usage in its events (examples/extensions/subagent/index.ts:300). The alternative is an in-process `createAgentSession({sessionManager: SessionManager.inMemory(), tools, customTools})` inside the forge tool, returning the summed usage. UNVERIFIED: the docs have no example of a session created inside an extension, and the child session needs its own `ModelRuntime`.

## PROPOSALS

1. **P1 (launcher plus guard):** use the flagged launcher and the fail-closed `session_start` guard from §1, not an SDK binary.
2. **P2 (no codemode):** exclude codemode. Expose the toolshed as `direct` while it is small; change to `deferred` plus `tool_search` above about 30 tools. If we need composition in one step, add a `compose` operation that runs inside the toolshed.
3. **P3 (register only through the gate):** the model never sees `register`. The gate code calls the toolshed `register` only after `ctx.ui.confirm` / `ctx.ui.custom`. The toolshed operations `exec`, `install` and `test` are visible only to the forge sub-agent.
4. **P4 (jailed file tools):** use `-nbt`, then register our own `read` and `write` tools with operations limited to `./workspace`. Pi exports `createReadTool` / `createWriteTool` with replaceable operations (gondolin example).
5. **P5 (thinner sandbox candidates):**
   - Gondolin (`@earendil-works/gondolin` 0.12.0, a QEMU micro-VM from the Pi team; docs/containerization.md)
   - bubblewrap through `@anthropic-ai/sandbox-runtime` (sandbox example)

   Rootless podman is still the simplest option for a module that holds a tool database.
6. **P6 (governor):** the governor ledger is the single source of cost. Pi's footer is a cross-check only, because it does not count the gap pass.

## RISKS

- **Extension injection:** the built-in `write`/`edit` tools can write `~/.pi/agent/extensions/*.ts` or `.pi/extensions`, and Pi loads those as code on the next start. `-ne` plus P4 closes this.
- **Key leak:** the built-in `read` can read `/proc/self/environ` and `auth.json`, so the key can enter the model's context and then toolshed arguments. Use P4. Keep the key in an environment variable or in `.frank/agent`.
- **Context files:** our repo's `CLAUDE.md` / `AGENTS.md` load into Taltempla's context unless we pass `-nc`.
- **Undocumented API:** `main(args, {extensionFactories})` and `InteractiveMode` are not in the docs, so they can change.
- **No built-in spend cap:** Pi has none, and `before_provider_request` cannot block. Only `ctx.abort()` and `input:handled` work, and the timing of `ctx.abort()` is UNVERIFIED.
- **MCP race:** with `deferred`, the first prompt does not wait for the toolshed to connect, so the gap pass must wait for it.
- **Cache loss:** each change to the tool list throws away DeepSeek's prompt cache, so cost rises.
- **Telemetry:** `enableInstallTelemetry` is on by default; set it to false.
