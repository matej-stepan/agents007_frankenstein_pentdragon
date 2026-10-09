# Open questions

Status: v7, 2026-10-08. Language: ASD-STE100. When we answer a question, we move the answer to the decision log in [ARCHITECTURE.md](ARCHITECTURE.md).
⭐ = this question blocks other work. PR-n = a proposal in ARCHITECTURE.md §13. En = an enforcement row in §5. Contract = [contract/INTERFACES.md](contract/INTERFACES.md).

## CLI
- **Long sessions:** The CLI has no compaction. What does it do when the context of a session gets near the model limit? Each kept assistant message must keep its `reasoning_content`.
- **Main agent effort:** Which `reasoning_effort` does the main agent use: the default, `low` or `high`? Measure the cost and the quality in the dry runs.
- **Cap reached:** A 402 stops the step. Can the operator raise a cap in the session, or only with `make run CAP_RUN=…`?
- **Reviewer switch:** How does the operator set P4 to `deepseek-v4-pro` (D41)? The contract has no variable for it.

## Toolshed
- **Lookup thresholds:** Which bm25 and coverage values give `good`, `partial` and `none` (D35)? We tune them in the dry runs, with the demo prompts. A wrong `good` stops a necessary build. A wrong `none` builds a tool that exists. Record the final values as a decision.
- **Repair input:** The `invoked` event does not record the arguments (D42). How does the repair build get the failed input for the regression test? Proposal: the main agent gives it in `need`.
- **Permissions `network` and `files`:** The gates show them, but the runner does not apply them (E8). Do we add a network namespace for `network: false` and a read-only `/work` for `files: read`, or is this LATER?

## Module boundary
- **Session 2 composition:** Brief definition of done 4 says "no rebuilding, no manual wiring". In session 2, fit `partial` makes the Big Chef build a new big tool from the session 1 small tools. Does the jury accept a new big tool as composition? Ask the mentor (rwngwn). Fallback: the main agent chains the small tools with `use_tool`.

## Inference
- **Account (PR-10):** The balance is USD 6.06 (preflight). Is it a dedicated account? Is it sufficient for the dry runs and the recording? Who adds money?
- **Prices:** Confirm the peak and off-peak prices in `prices.json` (contract §4) on the DeepSeek site. `/cost` compares the ledger with the balance change.

## Demo and twist
- **Data sources:** Which real-estate sources does session 1 use (for example sreality.cz, bezrealitky.cz, reality.idnes.cz)? Which car sources does session 2 use (for example sauto.cz, tipcars.com)? Do their terms of use and `robots.txt` permit scraping for a demo? Do they block datacenter IPs or need JavaScript? The "comprehensive" checklist needs 2 sources or a fallback.
- **Real failure:** Where does a real failure occur on video? We must not cut failures. The P3 test failures are real. The repair (§15 step 5) needs a real failed input, not a fake one.
- **Twist (PR-16):** Do we keep the Frankenstein role names, or only the Big Chef?
- **Voice (ElevenLabs, optional):** Do we do it if time is left? The CLI then holds a second key (D44 is only about tools).
