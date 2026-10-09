# Taltempla

A self-extending agent for hackathon Case 03 ("Frankenstein"). It has two modules. **Module A, the CLI**, is a Python program on the host: an agent loop with five fixed tools (`lookup`, `use_tool`, `big_chef`, `ws_read`, `ws_write`) and the meter, which is the only holder of the DeepSeek key and the single point for spend and caps. **Module B, the toolshed**, is one rootless Podman container: it keeps the tool registry, finds tools without an LLM, and runs the Big Chef, which plans, tests, codes and reviews new tools. All generated code executes there, as a low-privilege user. When lookup finds no good tool, the agent asks the Big Chef to build one. The operator approves the install, and the agent uses the tool in the same session. Later sessions reuse and compose the tools, and `/cost` shows what the reuse saved.

## Quickstart
You need a Linux host with rootless Podman, `.env` with `OAI_COMPATIBLE_KEY=…` (never commit it), and `toolshed/packages.json`. If a file is missing, ask the operator.

```sh
make deps          # print the apt line (podman passt uidmap), install uv if missing, uv sync
make doctor        # preflight checks; each failed check prints the fix
make toolshed-up   # build the image if missing, start the toolshed container
make run           # start the CLI (runs doctor, starts the toolshed if it is down)
make help          # list all targets
```

- Overrides: `make run CAP_RUN=1 CAP_SESSION=3 MODEL=deepseek-flash GATE=ask`.
- In the CLI: `/shed` (tools), `/cost` (spend and savings), `/allow` (the "always allow" list), `/rollback`.
- Other targets: `make shed`, `make cost`, `make toolshed-down`, `make toolshed-reset`, `make demo-reset`, `make test`.

**Data:** prompts, tool code and tool output go to the DeepSeek API, which stores data in the PRC and can use it for training. Do not enter personal data.

## Documents
- [PLAN.md](PLAN.md): the v7 plan.
- [ARCHITECTURE.md](ARCHITECTURE.md): the design and the decision log.
- [contract/INTERFACES.md](contract/INTERFACES.md): the interfaces between the parts (binding).
- [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md): open questions.
- [SUBJECT.md](SUBJECT.md): the hackathon brief.
