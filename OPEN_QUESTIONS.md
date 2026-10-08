# Open questions

Status: v6, 2026-10-08. Language: ASD-STE100. When we answer a question, we move the answer to the decision log in [ARCHITECTURE.md](ARCHITECTURE.md).
⭐ = this question blocks other work. PR-n = a proposal in ARCHITECTURE.md §13. En = an enforcement row in §5.

## CLI
- ⭐ **Strict mode:** Strict tool schemas need `https://api.deepseek.com/beta`. Does Pi send `strict`? Fallback: a named tool, a check in the CLI and one retry.
- **Fresh context (D23):** What enters the fresh context: the prompt, the gap list, the file references, a handoff note? Which role writes the handoff note?
- **Forge form:** Do we keep the loop of nested calls in `execute()`? The alternative is an in-process `createAgentSession()`.
- **Gap pass and triage:** How many recent turns enter the gap pass? When does the triage return `covered`, and when `build`?
- **Too many builds:** Are the triage, `resolve_gap`, the build cap and `/skip` sufficient?
- **Reviewer:** Only an LLM step, or also `ruff`? Which items are on the "malicious" checklist?
- **Gate:** What is the layout? It must show the build log with all test runs and the last draft.
- **Cap reached:** Does the run stop, or can the operator raise a cap with a command?
- **Launcher:** Does the TUI operate with only `PATH` and `TERM` (E6)?

## Toolshed
- ⭐ **Podman check on the dev machine (30 min):** A rootless container, data volume permissions, DNS for package installs, a mount of the exchange directory.
- **Tool user:** Which mechanism sets the limits: `timeout` and `ulimit`, or other?
- **Admin token:** Where does it live? Does each `make toolshed-up` make a new token?
- **Proposal:** Do we accept PR-4 (TypeScript server)?
- **Runtime:** Which system packages: `pandoc`, `typst`, fonts, `poppler`? Can the forge add one?
- **uv:** Which version do we pin? `--locked` for scripts needs uv 0.11.4 or later.
- **Seed tools:** Do we need seed tools at all? A fetch tool needs the network.
- **`invoked` events:** Do we record the call arguments? Arguments can contain personal data.

## Module boundary
- **Management tools (definition of done 3):** Confirm the demo prompt in §15 step 5. Which fields of the tool database can a tool read?
- **Tools that need a third-party key:** The prototype forbids them. PR-14 (key injection) comes later.
- **Compaction:** Does Pi compaction keep the `reasoning_content` of assistant messages? If not, a long session can fail with HTTP 400.

## Inference
- **`deepseek-v4-pro`:** The DeepSeek documents disagree about its end date. Do we accept PR-8 with the fallback?
- **Cap values:** Confirm USD 0.50 for each build, 2.00 for each run, 5.00 for each session and 20.00 in total. Confirm 3 builds and 5 iterations.
- **Account:** Do we accept PR-10? Who owns the account and the key? Which prepaid amount?
- **Model names:** Confirm `deepseek-flash` and `deepseek-v4-pro` on the DeepSeek site before the build.

## Demo and twist
- ⭐ **Twist:** Do we accept PR-15 (a price tag on each tool) and PR-16 (Frankenstein roles)?
- **Scenario:** Confirm: a CV, then a job application in a new session, then a management question in session 3.
- **Real failure:** Where does a real failure occur on video? We must not cut failures.
- **Persona:** Do we accept PR-11 (a synthetic persona)?
- **Voice (ElevenLabs, optional):** Do we do it if time is left? The CLI then holds a second key.
