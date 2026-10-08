<!-- Raw research notes from the planning workflow, 2026-10-08. Not ASD-STE100. Facts can change: verify before use. -->

# Toolshed technology brief (2026-10-08, planning only)

Facts about this Debian 13 box: `podman` 5.4.2 is in apt, subuid is set, `/dev/kvm` is usable by the user, and `bwrap` 0.12, Node 26 and Python 3.13 are installed. Docker and uv are not installed.

## (a) Sandbox

| Option | Setup / isolation | Limits, egress | Verdict |
|---|---|---|---|
| Rootless Docker | rootlesskit, setup script, user daemon, a socket that can leak | Needs a timeout wrapper | Current plan |
| **Rootless Podman** | `apt install podman`. No daemon, no socket. Host env only with `--env-host` | `--timeout --memory --cpus --pids-limit --read-only --cap-drop=all`. `--network=none\|pasta` for each call | **Default** |
| gVisor | Install changed 2026-07. Rootless with Podman not verified | Rootless network limited | Reject |
| Firecracker / Kata | Own rootfs and agent / rootful containerd | — | Reject |
| **microsandbox** (libkrun) | Installer script, KVM is here. Beta | Host allowlist, snapshots, MCP | **Alternative** (VM boundary, later) |
| nsjail | Build from source | rlimits, netns | Reject |
| bwrap / `srt` | Host kernel and files, reads allowed by default. Preview | Domain proxy | Reject: looks like "executes on the host" |
| e2b / Daytona self-host | Terraform+Nomad+PG+Redis+ClickHouse / about 11 compose services | — | Reject |

## (b) Execution API

| Option | Verdict |
|---|---|
| **Plain HTTP exec, stateless, file based.** One timeout for each call, maps 1:1 to `podman run` | **Default** |
| Jupyter kernel (jupyter-server). Stateful, but kill and timeout are difficult and state hides dependencies. Kernel Gateway: last release 2024-03 | **Alternative**, only as an optional `repl` operation |
| MCP code-exec server (microsandbox-mcp, container-use). Gives raw exec to the main agent | Reject for exec |

One daemon, `shedd`, with two surfaces:
- **Admin HTTP `/v1`** on a unix socket, for client extensions only: `exec install test put get diff register rollback snapshot list`.
- **MCP `/mcp`** (streamable HTTP, 127.0.0.1, token) for the main agent: one tool for each active registered tool, plus a read-only `toolshed_search`. No exec, register or rollback.

Each `exec`/`test`/`invoke` is a new `podman run --rm` and returns `{exit, stdout, stderr, duration_ms, timed_out, oom, files[]}`. Tests produce JUnit XML (`pytest --junitxml`, `node --test --test-reporter=junit`). shedd parses it to `{passed, failed, cases[]}` and keeps the log.

**Dependency conflicts:** a Python tool is one PEP 723 file plus `uv lock --script`. A Node tool is a directory with `pnpm-lock.yaml`, and Node tools share one store. shedd builds one env for each (tool, lock hash) (for example `uv sync --script --active --locked`, verify the flags), and `invoke` mounts it read-only. System binaries (pandoc, typst, fonts) are in the runtime image. A new system package makes a new image tag.

**Down/up reproducibility:** the truth is the locks, the Containerfile with `system-packages.txt`, and the image tag in each toolset snapshot. Envs and caches are disposable. `make toolshed-up` builds missing envs again.

## (c) Tool database

| Option | Verdict |
|---|---|
| **git repo of tool directories + SQLite index.** Native diffs for the gate, tags as versions, commit trailers for provenance, SQL and FTS5 queries. The index can be rebuilt from git | **Default** |
| SQLite only, append-only, code as blobs. One store, but we must write the diff and snapshot code | **Alternative** |
| Dolt. Needs a server binary, code in rows is difficult to diff and execute, duplicates git | Reject |

Layout: `registry/tools/<name>/{manifest.json, tool.py, tool.py.lock, tests/}`. Tags: `tool/<name>/vN` and `toolset/N`. History only goes forward: a rollback is a new commit that restores an old tree. Provenance comes from the manifest `origin`, the commit author (`seed`/`forge`) and trailers (`Origin, Build, Test-Run, Content-Hash, Approved-By, Cost-USD`). SQLite tables: `tools`, `versions`, `test_runs`, `approvals`, `toolsets` (with the image tag) and `events`. All are append-only except the active pointer.

## PROPOSALS

- **P1. Use Podman, not rootless Docker.** It installs from apt, has no daemon or socket, and has a built-in `--timeout`.
- **P2. The control plane is outside the sandbox.** shedd is our trusted code: it runs on the host, starts with `env -i`, has no keys and owns the registry. Generated code runs only in per-call containers, which never get a writable mount of the registry. So "authority may not grow" is part of the structure. Cost: about 0.3–1 s for each call. If the toolshed must be one container: run shedd as uid `shed`, run drafts as uid `draft` (`setpriv`, `prlimit`, `timeout`), make the registry mode 0700, and accept one network setting for the full container.
- **P3. Declared permissions are container flags:** `net` → `pasta`, else `none`. Files → `/work`, plus `/tool` read-only. Time, memory and pids come from the manifest, and the governor sets the maximums. The gate shows the flags.
- **P4. Installs use the network, runs are offline by default,** unless the manifest declares `net`.
- **P5. Registration is bound to a content hash.** shedd registers only when a passed `test_run` exists for the same hash and image tag. The gate approval names that hash.
- **P6. Write shedd in TypeScript.** It shares zod contract schemas with the client and gets the MCP TS SDK (`list_changed`), `node:sqlite` and the `git` CLI. Tools stay mostly in Python.
- **P7. Connect with Pi core MCP** (0.99/1.0). Keep a fallback extension that registers the tools as native Pi tools over HTTP.

## RISKS

- Pi core MCP is about 10 days old. We do not know if `tool_call` hooks see MCP calls (the governor and gate need this), or if `list_changed` refreshes tools during a session. Codemode runs model JavaScript in QuickJS on the host: turn it off or accept it.
- pasta can maybe let a runner reach host loopback. So: admin API on a unix socket, and a token that runners never get.
- When the network is on, it is fully open. This is acceptable because there are no credentials inside.
- Rootless Podman problems: volume uid mapping (`:U`/`keep-id`), pasta DNS, the first image build. Do a 1-hour spike first.
- A draft can poison the shared caches during install. Lock hashes and read-only envs reduce this.
- A rollback must restore the image tag and the envs, not only the code.
- uv script flags change between versions (`--locked` is enforced only from 0.11.4). Pin uv.

Sources: [Pi 0.99 MCP](https://ai-tldr.dev/releases/earendil-pi-0-99-mcp-codemode/), [podman-run](https://man.archlinux.org/man/podman-run.1.en.raw), [Debian podman](https://packages.debian.org/src:podman), [microsandbox](https://github.com/microsandbox/microsandbox), [srt](https://github.com/anthropic-experimental/sandbox-runtime), [gVisor install](https://groups.google.com/g/gvisor-users/c/ZEAMsobSSiI), [e2b self-host](https://raw.githubusercontent.com/e2b-dev/infra/main/self-host.md), [Daytona OSS](https://www.daytona.io/docs/en/oss-deployment/), [uv script locks](https://pydevtools.com/handbook/how-to/how-to-lock-uv-script-dependencies/), [uv #11433](https://redirect.github.com/astral-sh/uv/pull/11433), [Kernel Gateway](https://jupyter-kernel-gateway.readthedocs.io/)

Draft copy: /tmp/claude-1000/-home-truecat-dny-10-hackathon01/2c6e364e-721c-4687-ade4-a15fe4468b02/scratchpad/brief.md
