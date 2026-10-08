# Case 03 — Frankenstein

> **Build an agent that can build itself:** recognize what capability it's missing, create it, test it, install it, and use it again later.
>
> `CREATE → TEST → INSTALL → EVOLVE`

| | |
|---|---|
| **Topic partner** | Etnetera (proposed by the organizers) |
| **Mentors / jury** | David Bečvařík (Etnetera, Discord: `rwngwn`) · Luděk Šafář (prg.ai) |
| **Side challenge** | Best ElevenLabs Use (optional, judged separately by Vláďa Beran) |

---

## The real problem

Writing a tool to finish one task is not enough. The agent must build and evolve an
**ecosystem** that makes its capabilities **discoverable, composable and manageable across sessions**.

> **Its capabilities may grow; its authority may not.**

## Definition of done (must work)

1. **Session 1:** A task exposes a missing capability.
2. The agent **creates, tests and registers** it, then **completes the task**.
3. The agent also **builds or extends the tooling** for discovering and managing its capabilities.
4. **Session 2 (fresh):** A *different* task **combines previously generated capabilities** —
   no rebuilding, no manual wiring.

This is the bare skeleton, not the ceiling — expand it, reframe it, add a twist.

---

## Scope

### ✅ In bounds
- **Gap detection** — the agent knows what it cannot do
- Generating **code tools, MCP servers or skills**
- **Automatic tests** before install
- A **persistent tool registry** with versions and rollback
- **Agent-built** discovery and management tooling
- Eyes, hands or a voice *(optional)*

### ❌ Out of bounds
- Tools pre-written by the team
- Picking from a fixed tool library (routing ≠ building)
- Fine-tuning model weights
- Rewriting its own core loop or system prompt **without tests**
- Toy capabilities (adding numbers, reversing strings)
- Demonstrating only a harness's built-in features

### 🔒 Hard rules (non-negotiable)
1. Generated code runs in a **sandbox**, never on a host holding credentials.
2. **No install without passing tests**; the test run is **visible in the log**.
3. The gap must **come from a task** — no hardcoded "now build tool X".
4. **Self-iterations and spend per run are capped in code.**

---

## Real vs. faked

- The gap must be real: **show the tool registry before the run**.
- "Generated" code that the team wrote or seeded is **the one unforgivable fake**.
- In the video: speed up waiting, **never cut failures**.

## What wins

A **useful, agent-built ecosystem**, not a one-off script. Show:
- **creation**
- **fresh-session composition**
- **real operator control**

---

## FAQ (decided in advance)

| Question | Answer |
|---|---|
| Can we start from a framework (OpenClaw, Claude Agent SDK, LangGraph)? | **Yes** — the self-extension loop is what gets judged. |
| Does installing a skill from a marketplace count? | Counts as *install*, not *create*. **At least one capability in the demo must be agent-written.** |
| What can a capability be? | A code tool, an MCP server or a prompt-skill — with **explicit interfaces, declared permissions and executable tests**. |
| Can a human approve the install? | **Yes, an approval gate is encouraged.** Detection, creation and testing stay with the agent. |
| Anything not covered? | Your call. |

## Mentor clarifications

| Date | Question | Answer |
|---|---|---|
| 2026-10-08 | Hard rule 1 — do we literally need a remote sandbox? | **No.** The agent just must not see the keys directly. Ideally, inject them. *(rwngwn)* |

## Side challenge — Best ElevenLabs Use

Optional: give the agent a voice with ElevenLabs. One small extra prize. Tick the box on submission to compete.

## Mentors

David is on site generally until midnight — he sleeps at the venue, so you can try to wake him up even after ;-)
