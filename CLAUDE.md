# Taltempla (hackathon Case 03)

A self-extending agent: two modules, the CLI (on Pi, DeepSeek only) and the toolshed (local Podman sandbox and tool database).

## Documents
- `SUBJECT.md`: the hackathon brief. `subject.txt` is the original.
- `ARCHITECTURE.md`: the design and the decision log. Read it before you suggest a change.
- `OPEN_QUESTIONS.md`: open questions. ⭐ = a blocker.
- `research/`: raw research notes (Pi, DeepSeek, toolshed). They are not a decision.

## Conventions
- Write the design documents in ASD-STE100 and keep them concise.
- Do not open decisions in the decision log again. Add a new decision that replaces the old one.
- When we answer a question, move the answer to the decision log.
- A Makefile dispatches all tasks.
- Work only in this directory. Do not read `../Taltempla/` or another sibling directory. If a local file (for example `.env`) is missing, ask the operator.
