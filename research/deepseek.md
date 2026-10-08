<!-- Raw research notes from the planning workflow, 2026-10-08. Not ASD-STE100. Facts can change: verify before use. -->

**DeepSeek API brief (verified 2026-10-08)**

Source key: [P] /quick_start/pricing · [CL] /updates · [N] /news/news260910 · [TM] /guides/thinking_mode · [TC] /guides/tool_calls · [JM] /guides/json_mode · [KV] /guides/kv_cache · [API] /api/create-chat-completion · [BAL] /api/get-user-balance · [ANT] /guides/anthropic_api · [RESP] /guides/responses_api · [RL] /quick_start/rate_limit · [ERR] /quick_start/error_codes. All of these are under https://api-docs.deepseek.com. Other sources: [FAQ] https://static.deepseek.com/faq/index.html?lang=en · [TOS] https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html · [PP] https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html · [PI] npm @earendil-works/pi-ai@1.1.0, `dist/providers/data/deepseek.json` and `dist/api/openai-completions.js`.

## 1. Models [P][CL][API]
| ID | Serves | Ctx | Max out | Notes |
|---|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash (2026-09-10, 552B MoE) | 1M | 384K (393216) | vision yes, concurrency 2500 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro-0813 | 1M | 384K | no vision, concurrency 500 |

- There are no separate chat and reasoner models any more. Each model has a thinking mode that you switch with `thinking:{type:enabled|disabled}`. Thinking is on by default.
- `reasoning_effort` takes `none|low|high|max`, default `high`. The default `max_tokens` is 8K in non-thinking mode, 64K in thinking mode and 128K with `max` effort.
- `deepseek-chat` and `deepseek-reasoner` stopped working on 2026-07-24. `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` are now aliases for V4.1-Flash.
- The docs conflict on V4-Pro:
  - [N] says Pro requests go to Flash from 2026-09-14.
  - [CL] and [P] say Pro service continues "until further notice", billed as before.
  - So we must log the `model` field of every response.
- V4.1-Flash scores higher than V4-Pro on the agentic coding benchmarks [CL]: Terminal-Bench 2.1 90.6 vs 87.9, DeepSWE 74.2 vs 62.7.

## 2. Pricing (USD per 1M tokens) [P]
| | Flash peak | Flash off-peak | Pro peak | Pro off-peak |
|---|---|---|---|---|
| Input, cache hit | 0.006 | 0.003 | 0.044 | 0.022 |
| Input, cache miss | 0.30 | 0.15 | 1.32 | 0.66 |
| Output | 1.20 | 0.60 | 3.96 | 1.98 |

- Peak hours are 01–04 and 06–10 UTC, Monday to Friday, except Chinese public holidays. Off-peak is 50% of peak. Prices "may change at any time".
- Cost of one call = `prompt_cache_hit_tokens`×hit + `prompt_cache_miss_tokens`×miss + `completion_tokens`×output.
- `prompt_tokens` = hit + miss. `prompt_tokens_details.cached_tokens` = hit [API].
- `completion_tokens_details.reasoning_tokens` is a breakdown only. It is already counted inside `completion_tokens` (that is how Pi treats it [PI]; [API] does not state it).
- Caching is automatic and best-effort. Only a full prefix-unit match counts as a hit. A cache lives for hours to days [KV].
- In streaming mode, usage needs `stream_options.include_usage` [API].

## 3. Tools and structured output [TC][TM][API][JM][RESP][ANT]
- **Tool calls:** both models support them, in both thinking and non-thinking mode. In thinking mode the model can reason, call a tool, and reason again before its final answer.
- **Hard rule with tools in thinking mode:** you must send back every earlier `reasoning_content`. If you do not, the API returns 400.
- **`tool_choice`:** takes `none|auto|required|{named}`. In thinking mode, `required` and named choices return 400.
- **Ignored in thinking mode:** `temperature` and the penalty parameters.
- **Strict mode (beta):**
  - It needs base URL `https://api.deepseek.com/beta` and `strict:true` on each function.
  - Every object needs all properties in `required` and `additionalProperties:false`.
  - `minLength`, `maxLength`, `minItems` and `maxItems` are not supported.
  - It works in both modes.
- **JSON mode:** only `response_format:{type:json_object}`. There is no `json_schema`. The prompt must contain the word "json". The docs say the API "may occasionally return empty content".
- **Parallel tool calls:** you cannot switch them off. `parallel_tool_calls` and `disable_parallel_tool_use` are ignored.

## 4. Endpoints and spend control
- **OpenAI-compatible base URL:** `https://api.deepseek.com`. Beta features use `/beta`. The Responses API is supported but stateless (`store:false`, no `previous_response_id`) [RESP].
- **Anthropic-compatible base URL:** `https://api.deepseek.com/anthropic`, `/messages` only [ANT].
  - `claude-opus*` model names go to v4-pro. All other names go to flash.
  - `cache_control` and `budget_tokens` are ignored.
- **`GET /user/balance`** (Bearer auth) returns `is_available` and `balance_infos[]` with `currency` (CNY or USD), `total_balance`, `granted_balance` and `topped_up_balance` [BAL].
  - The docs do not say how quickly the balance updates. The balance covers the whole account, so all keys count.
  - It is good as a cross-check: snapshot it at run start and run end and compare the change with our ledger, within a tolerance.
  - It is too slow to be the cap.
- **Account spend limit:** none is documented [FAQ]. The only provider-side stop is the prepaid balance: when it is empty, calls return 402 [ERR]. The Usage page can export usage per API key [FAQ].

## 5. Rate limits, quirks, privacy
- **Limits:** concurrency is counted per account, across all keys. Over the limit you get 429. No RPM or TPM limits are documented, and there are no paid tiers. Higher limits are free on request [RL][FAQ].
- **`user_id`** (body field, `[A-Za-z0-9_-]`, no personal data) gives separate KV-cache and scheduling isolation [RL].
- **Quirks:**
  - While a request waits, the server sends keep-alive empty lines (non-stream) or `: keep-alive` comments (stream).
  - The server closes the connection if inference has not started after 10 minutes.
  - `finish_reason` can also be `insufficient_system_resource` or `aborted`.
  - Errors: 400, 401, 402, 422, 429, 500, 503 [ERR].
  - Thinking mode at `high` adds latency and output tokens.
- **Privacy:**
  - The Open Platform ToS (effective 2026-04-29) §3.3: the developer must tell end users and get their consent, including for "delegation of personal information processing to us" [TOS].
  - The Privacy Policy (2026-02-10) [PP]:
    - Data is stored in the PRC.
    - Personal data is used "to train and improve our technology".
    - There is no fixed retention period.
    - "Do not provide sensitive Personal Data". Sensitive data includes health, citizenship, biometrics and children's data. A CV can contain these.

## 6. Role assignment (all through Pi's native `deepseek` provider)
| Role | Model / mode | Why |
|---|---|---|
| Main agent | flash, thinking `high` (`low` for chat replies) | strong agentic scores; cheap cached context |
| Gap pass | flash, **non-thinking**, named `tool_choice` + strict (/beta), max_tokens about 1.5K | forced schema output is legal only in non-thinking mode; fast; costs about $0.002 |
| Stack pass | flash, thinking `low`; runtime field is an `enum` filled from the toolshed runtime list | short and bounded |
| Test writer | flash, thinking `high`, **fresh context: spec and examples only, never the code** | black-box tests, so tests do not copy the generator's bugs |
| Generator (forge loop) | flash, thinking `high`, max_tokens about 16K per call | best coding model available |
| Reviewer | **v4-pro**, thinking `high`, fresh context (spec, manifest, code, test log), adversarial prompt, strict verdict schema, its own `user_id` | Different model, generation and weights, so it has different blind spots. Fallback when Pro goes away: flash `max` with the same context isolation. Temperature diversity does not work, because thinking mode ignores it. |

**Estimated cost of one build** (Flash at peak, Pro reviewer, about 85% cache hits in the generator loop):

| Step | Cost |
|---|---|
| Gap pass | $0.002 |
| Stack pass | $0.004 |
| Test writer | $0.011 |
| Generator, about 8 turns, 240K cumulative input, 32K output | $0.05 |
| Reviewer | $0.05 |
| **Total** | **about $0.12** |

- About $0.08 with a Flash reviewer.
- About half of each figure off-peak.

## PROPOSALS
- **P1: Use `@earendil-works/pi-ai` for every LLM call, not the OpenAI SDK.** Pi 1.1.0 already ships a `deepseek` provider [PI]: env `DEEPSEEK_API_KEY`, `thinkingFormat:"deepseek"`, `requiresReasoningContentOnAssistantMessages:true`, `supportsStrictMode:true`. It reads both cache-token field names. That gives one usage pipeline and one Governor hook point. The architecture doc says Pi 1.0; the current version is 1.1.0.
- **P2: The Governor computes USD itself, using the peak/off-peak table and the request timestamp.** Pi's catalog uses peak prices only, so off-peak it reports 2× the real cost. Keep Pi's number as a conservative "list" column.
- **P3: Reserve budget before each call, settle after.**
  - Before: block the call if prompt_est×miss + `max_tokens`×output is more than the remaining budget.
  - After: settle from the `usage` fields.
  - This needs an explicit `max_tokens` for every role. The 64K thinking default would make the worst case big.
- **P4: Provider-side backstop.**
  - Use a dedicated DeepSeek account with a small top-up (for example $10).
  - Snapshot `/user/balance` at run start and run end. Show the difference next to the ledger in the operator view.
  - Optional: one API key per role, so the Usage export checks the ledger per role.
- **P5: Default caps:** $0.50 per build, $2 per run.
- **P6: Use a synthetic CV persona for the demo, and state DeepSeek data handling in the README.**
- **P7: Gap-pass output goes into the context as text or a system message.** Never as a synthetic tool call; Chat Completions rejects inserted tool calls mid-conversation [TC].

## RISKS
- **V4-Pro may be removed.** The docs disagree. The reviewer needs the flash-`max` fallback, and a config switch for it.
- **Strict mode on Pi:** Pi's base URL is not `/beta`, but strict needs `/beta`. Strict may be silently off. Test it early.
- **`reasoning_content` 400:** any extension that rewrites or trims assistant messages can drop `reasoning_content` and cause 400 errors.
- **Parallel tool calls are always on.** The Governor and the Gate must serialize forge and build requests themselves.
- **JSON mode empty content:** prefer strict tool calls, and retry once.
- **Off-peak rate attribution** (start time or end time of the request) is not documented. The ledger can be off by up to 2× for calls that cross a peak boundary. P4 cross-checks this.
- **No documented account spend cap.** The only provider-side stop is the prepaid balance.
- **CV data goes to the PRC and may be used for training.** That is a jury and privacy optics issue.
- **Agent-written tools that need an LLM cannot hold the key, because the toolshed has no credentials.** Forbid such tools for now, or design a host-mediated `llm` capability. This is a new boundary decision.
- **Prices change without notice.** Keep the price table in config, not in code.
