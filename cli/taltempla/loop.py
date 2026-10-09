"""The agent loop: one byte-stable history per session, a fixed toolset, one run grant per prompt."""

import contextlib
import http.client
import json
import secrets
from collections import deque
from pathlib import Path

from . import gate, llm, ui, waiter
from .approvals import Approvals
from .meter import MeterRefused
from .shed_client import ShedClient, ShedError
from .ws_tools import WsError, ws_read, ws_write

MAX_TURNS = 40
MAX_BUILDS = 2  # big_chef calls per user prompt (a hard cap in code, rule 4)
MAX_RESULT = 6144
RUN_RESERVE_USD = 0.10  # kept from a build cap: one Waiter call + the main agent's answer (D58)
MIN_BUILD_USD = 0.10  # a smaller cap cannot pay the first P1 call (4096-token floor)
CANCELLED = "the operator cancelled the build. Answer with the installed tools and state what is missing."
PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")

_S = {"type": "string"}
TOOLS = [
    llm.tool_schema(
        "lookup",
        "Search the toolshed (no LLM). query -> lookup_id, fit good|partial|none, <=3 big tools. "
        "tool -> one tool's SKILL.md and input schema.",
        {"type": "object", "properties": {"query": _S, "tool": _S}},
    ),
    llm.tool_schema(
        "use_tool",
        "Run an installed big tool in the sandbox (small tools are building blocks for the Chef). "
        "args must match its input schema.",
        {
            "type": "object",
            "properties": {"name": _S, "args": {"type": "object"}},
            "required": ["name", "args"],
        },
    ),
    llm.tool_schema(
        "big_chef",
        "Build, test and install a new tool for a missing capability. Needs a lookup_id with fit none|partial "
        "from this session. repair_of {tool, invoke_id} repairs a failed run; add problem to improve a run whose "
        "result has problems (the CLI adds that run's args).",
        {
            "type": "object",
            "properties": {
                "task": _S,
                "need": _S,
                "lookup_id": _S,
                "repair_of": {
                    "type": "object",
                    "properties": {"tool": _S, "invoke_id": _S, "problem": _S},
                    "required": ["tool", "invoke_id"],
                },
            },
            "required": ["task", "need", "lookup_id"],
        },
    ),
    llm.tool_schema(
        "ws_read",
        "Read a text file (<=64 KB) or list a directory in workspace/.",
        {"type": "object", "properties": {"path": _S}, "required": ["path"]},
    ),
    llm.tool_schema(
        "ws_write",
        "Write a text file in workspace/ (makes parent directories).",
        {"type": "object", "properties": {"path": _S, "content": _S}, "required": ["path", "content"]},
    ),
]


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}"


def chain_llm_cap(name: str, tools: dict) -> float:
    """USD cap of one root run: the llm_usd of the tool plus every tool it can reach through `uses`."""
    seen, todo, cap = set(), [name], 0.0
    while todo:
        n = todo.pop()
        if n in seen or n not in tools:
            continue
        seen.add(n)
        cap += float((tools[n].get("permissions") or {}).get("llm_usd") or 0)
        todo += tools[n].get("uses") or []
    return round(cap, 6)


def stale_gap(lookup_id: str, lookups: list[tuple[str, str, list]]) -> list[str]:
    """The good-fit tools of lookups newer than lookup_id. Non-empty = the gap evidence is stale (rule 4)."""
    ids = [x[0] for x in lookups]
    if lookup_id not in ids:
        return []
    return sorted({n for _, fit, good in lookups[ids.index(lookup_id) + 1:] if fit == "good" for n in good})


def smoke_problems(result) -> list[str]:
    """Cheap checks of an ok result: empty results, a field that is null in every row (2+ rows), warnings."""
    if result in (None, "", [], {}):
        return ["the result is empty"]
    if not isinstance(result, dict):
        return []
    out = []
    if "results" in result and not result["results"]:
        out.append("results is empty")
    for key, rows in result.items():
        if key in ("warnings", "sources") or not isinstance(rows, list) or len(rows) < 2:
            continue
        if all(isinstance(r, dict) for r in rows):
            nulls = [f for f in dict.fromkeys(k for r in rows for k in r) if all(r.get(f) is None for r in rows)]
            out += [f"{key}[].{f} is null in all {len(rows)} rows" for f in nulls]
    if result.get("warnings"):
        out.append("warnings: " + compact(result["warnings"], 300))
    return out


def compact(result, limit: int | None = MAX_RESULT) -> str:
    s = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if limit and len(s) > limit:
        s = s[:limit] + f"...[truncated {len(s) - limit} chars]"
    return s


class Agent:
    def __init__(
        self,
        *,
        meter,
        shed: ShedClient | None,
        session_grant: str,
        session_id: str,
        root: Path,
        approvals: Approvals,
        model: str,
        caps: dict,
        effort: str = "high",
        build_defaults: dict | None = None,
    ):
        self.meter, self.shed, self.session_grant, self.session_id = meter, shed, session_grant, session_id
        self.root, self.workspace, self.approvals = root, root / "workspace", approvals
        self.model, self.caps, self.effort = model, caps, effort
        # G2/G3 defaults of each build; gate.build_options clamps them (a bad value falls back, never raises)
        self.build_defaults = {**gate.BUILD_DEFAULTS, "cap_usd": caps.get("build", 1.0), **(build_defaults or {})}
        self.messages: list[dict] = [{"role": "system", "content": PROMPT}]
        self.built: set[tuple[str, int]] = set()  # (tool, version) built in this session
        self.saved: set[tuple[str, int]] = set()  # (tool, version) already counted as reuse
        self.saved_usd = 0.0
        self.run_grant: str | None = None
        self.run_id: str | None = None
        self.builds = 0
        self.lookups: list[tuple[str, str, list]] = []  # (lookup_id, fit, good tool names) in this session
        self.invocations: dict[str, dict] = {}  # invoke_id -> args of each use_tool call in this session

    # ---- one user prompt -------------------------------------------------------------------------
    def run(self, prompt: str) -> bool:
        """Run one prompt to an answer. True = the model answered."""
        self.run_id, self.builds = new_id("r"), 0
        self.run_grant = self.meter.grant(
            "run",
            parent=self.session_grant,
            cap_usd=self.caps["run"],
            label=ui.short(prompt, 60),
            run_id=self.run_id,
        )
        self.messages.append({"role": "user", "content": prompt})
        try:
            for _ in range(MAX_TURNS):
                body = {
                    "model": self.model,
                    "messages": self.messages,
                    "tools": TOOLS,
                    "reasoning_effort": self.effort,
                    "user_id": self.session_id,
                }
                with ui.spinner("thinking…"):
                    resp = self.meter.chat(self.run_grant, body, role="main")
                msg = llm.assistant_message(resp)
                self.messages.append(msg)
                calls = llm.tool_calls(resp)
                if not calls:
                    ui.answer(msg["content"])
                    return bool(msg["content"].strip())
                if msg["content"].strip():
                    ui.dim(msg["content"].strip())
                for cid, name, args in calls:
                    out = self.call_tool(name, args)
                    limit = None if name == "ws_read" else MAX_RESULT
                    self.messages.append(
                        {"role": "tool", "tool_call_id": cid, "content": compact(out, limit)}
                    )
            ui.warn(f"run stopped: {MAX_TURNS} turns without an answer")
            return False
        except MeterRefused as e:
            i = getattr(e, "info", {}) or {}
            ui.error(
                f"spend cap reached: {i.get('cap', '?')} cap ${float(i.get('limit') or 0):.2f}, "
                f"spent ${float(i.get('spent') or 0):.4f}, next call needs ~${float(i.get('need') or 0):.4f}. "
                "Run ended."
            )
            return False
        except llm.LLMError as e:
            ui.error(f"LLM error {e.status}: {ui.short(str(e.body), 300)}")
            return False
        except KeyboardInterrupt:
            ui.warn("interrupted")
            return False
        finally:
            self._close_dangling()
            self.meter.revoke(self.run_grant)
            self.run_grant = None

    def _close_dangling(self) -> None:
        """Give every tool call of the last assistant message a reply, so the next request is valid."""
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m["role"] == "assistant":
                done = {x.get("tool_call_id") for x in self.messages[i + 1 :] if x["role"] == "tool"}
                for c in m.get("tool_calls") or []:
                    if c["id"] not in done:
                        self.messages.append(
                            {"role": "tool", "tool_call_id": c["id"], "content": '{"error":"run ended"}'}
                        )
                return

    # ---- tools --------------------------------------------------------------------------------------
    def call_tool(self, name: str, args):
        if not isinstance(args, dict):
            return {"error": f"arguments are not a JSON object: {ui.short(args, 120)}"}
        fn = {
            "lookup": self.t_lookup,
            "use_tool": self.t_use_tool,
            "big_chef": self.t_big_chef,
            "ws_read": self.t_ws_read,
            "ws_write": self.t_ws_write,
        }.get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return fn(args)
        except ShedError as e:
            ui.error(f"  {name}: {e}")
            return {"error": e.msg, "status": e.status}
        except WsError as e:
            ui.warn(f"  {name}: {e}")
            return {"error": str(e)}

    def t_lookup(self, a: dict):
        if self.shed is None:
            ui.dim("  lookup → toolshed offline (chat-only mode)")
            return {
                "lookup_id": None,
                "fit": "none",
                "rows": [],
                "note": "toolshed offline: no tools, no builds",
            }
        if a.get("tool"):
            r = self.shed.lookup_tool(a["tool"])
            ui.dim(f"  lookup tool={a['tool']} → v{r.get('version', '?')}")
            return r
        q = a.get("query") or ""
        r = self.shed.lookup(q, self.session_id)
        good = [x["name"] for x in r.get("rows", []) if x.get("fit") == "good"]
        self.lookups.append((r.get("lookup_id") or "", r.get("fit") or "none", good))
        rows = ", ".join(
            f"{x['name']} v{x.get('version', '?')} ({x.get('fit', '?')})" for x in r.get("rows", [])
        )
        ui.dim(f"  lookup “{ui.short(q, 60)}” → fit={r.get('fit')} · {rows or 'no tools'}")
        return r

    def t_ws_read(self, a: dict):
        ui.dim(f"  ws_read {a.get('path')}")
        return ws_read(self.workspace, a.get("path", ""))

    def t_ws_write(self, a: dict):
        r = ws_write(self.workspace, a.get("path", ""), a.get("content", ""))
        ui.dim(f"  ws_write {r['path']}")
        return r

    def t_use_tool(self, a: dict):
        name, targs = a.get("name") or "", a.get("args") or {}
        if self.shed is None:
            return {"error": "toolshed offline (chat-only mode)"}
        tools = {t["name"]: t for t in self.shed.tools()}
        tool = tools.get(name)
        if tool is None:
            return {"error": f"no tool named {name!r}; call lookup first"}
        if tool.get("grade") != "big":  # D53; shedd /invoke refuses it too. Checked before the use gate asks.
            ui.warn(f"  use_tool refused: {name} is a small tool")
            return {"error": f"{name} is a small building block. Only big tools run for the agent: use lookup, or "
                             "big_chef to build a task tool.", "status": 400}
        if not self.approvals.take(tool.get("perm_hash", "")):
            pick = gate.use(tool)
            if pick == "deny":
                ui.warn(f"  denied: {name}")
                return {"denied": True, "note": "the operator denied this tool run; do not retry it"}
            if pick == "always":
                self.approvals.allow_always(tool["perm_hash"], name, tool.get("version"))
        cap = chain_llm_cap(name, tools)
        g = self.meter.grant("tool_run", parent=self.run_grant, cap_usd=cap, label=name, tool=name)
        try:
            with ui.spinner(f"running {name}…"):
                r = self.shed.invoke(name, targs, g, self.run_id, self.session_id)
        finally:
            self.meter.revoke(g)
        if r.get("invoke_id"):
            self.invocations[r["invoke_id"]] = targs
        problems = smoke_problems(self._full_result(r)) if r.get("ok") else []
        if problems:  # near the front: the reply to the model is cut at MAX_RESULT chars
            r = {"ok": r["ok"], "problems": problems, **r}
        sub = r.get("subcalls") or []
        line = f"  {'✓' if r.get('ok') else '✗'} {name} v{r.get('version', '?')} · {r.get('duration_ms', 0) / 1000:.1f}s"
        if sub:
            line += f" · {len(sub)} subcalls ({', '.join(sorted({s['name'] for s in sub}))})"
        if r.get("refused_subcalls"):
            line += f" · {r['refused_subcalls']} subcalls refused (chain limits)"
        if r.get("llm_cost_usd"):
            line += f" · LLM ${r['llm_cost_usd']:.4f}"
        if r.get("out_path"):
            line += f" · → {r['out_path']}"
        if not r.get("ok"):
            line += f" · {ui.short(r.get('error', ''), 120)}"
        (ui.ok if r.get("ok") else ui.error)(line)
        if problems:
            ui.warn("  problems: " + ui.short("; ".join(problems), 200))
        if r.get("ok"):
            self._saving(name, r.get("version") or tool.get("version"), tool.get("build_cost_usd"))
            for s in sub:
                st = tools.get(s["name"]) or {}
                self._saving(s["name"], s.get("version"), st.get("build_cost_usd"))
        return r

    def _full_result(self, r: dict):
        """The result, or the JSON file shedd wrote to workspace/out/ for a large result (data only, never code)."""
        if "result" in r or not r.get("out_path"):
            return r.get("result")
        try:
            p = (self.root / r["out_path"]).resolve()
            if p.is_relative_to(self.workspace.resolve()) and p.stat().st_size < (32 << 20):
                return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return r.get("preview") or None

    def t_big_chef(self, a: dict):
        if self.shed is None:
            return {"failed": "toolshed offline (chat-only mode)"}
        rep = a.get("repair_of") or None
        if rep is not None and not isinstance(rep, dict):
            return {"failed": "repair_of must be an object {tool, invoke_id, problem?}"}
        stale = None if rep else stale_gap(a.get("lookup_id", ""), self.lookups)
        if stale:
            ui.warn(f"  big_chef refused: a newer lookup found a good fit ({', '.join(stale)})")
            return {"failed": f"stale lookup_id: a newer lookup found fit=good ({', '.join(stale)}). Use that tool, "
                              "or call lookup for the exact missing capability and pass its lookup_id."}
        if self.builds >= MAX_BUILDS:
            ui.warn(f"  big_chef refused: {MAX_BUILDS} builds per prompt")
            return {"failed": f"build limit: {MAX_BUILDS} builds per prompt. Answer now with the installed tools "
                              "and state what is missing."}
        body = {
            "task": a.get("task", ""),
            "need": a.get("need", ""),
            "lookup_id": a.get("lookup_id", ""),
            "session_id": self.session_id,
        }
        if rep:  # the model names the run; the CLI attaches the args it recorded (the model never supplies them)
            iid = str(rep.get("invoke_id") or "")
            body["repair_of"] = {"tool": rep.get("tool"), "invoke_id": iid, "problem": str(rep.get("problem") or ""),
                                 "args": self.invocations.get(iid)}
        what = (
            f"{'improve' if rep.get('problem') else 'repair'} {rep.get('tool')}"
            if rep
            else f"“{ui.short(a.get('need', ''), 70)}”"
        )
        opts, counted, cost, advice, out, resume, ck = self.build_defaults, False, 0.0, "", None, None, None
        while True:  # one pass per operator-approved attempt (G1 retry); MAX_BUILDS counts this call once
            left = self._run_left()
            room = round(left - RUN_RESERVE_USD, 4)
            if room < MIN_BUILD_USD:
                ui.warn(f"  big_chef refused: the run budget is too low for a build (${left:.2f} left)")
                return out or {"failed": f"the run budget is too low for a build (${left:.2f} left). Answer with "
                                         "the installed tools and state what is missing."}
            opts = gate.build_options(opts, what, room)
            if opts is None:  # before the first build, or at a retry (then the last failure goes back)
                ui.warn("  build cancelled by the operator")
                return out or {"failed": CANCELLED}
            ui.info(f"  big chef: building {what} · {gate.options_text(opts)}" + (" · with advice" if advice else "")
                    + (f" · resume of {resume}" if resume else ""), "cyan")
            extra = ({"advice": advice} if advice else {}) | ({"resume_of": resume} if resume else {})
            try:
                handoff, failed, events, started, nck = self._chef_stream(
                    body | extra, opts, ui.short(a.get("need", ""), 60), count=not counted)
            except ShedError as e:  # a refused retry keeps the earlier failure, its cost and its diagnosis
                if out is None:
                    raise
                ui.error(f"  chef retry refused: {ui.short(e.msg, 200)}")
                return {**out, "failed": f"{out['failed']} (retry refused: {ui.short(e.msg, 200)})"}
            counted = counted or started
            if nck["plan"] or not resume:  # a resume without its own plan checkpoint keeps the older checkpoint
                ck = nck
            if handoff and not failed:
                return self._install(handoff)
            reason = str((failed or {}).get("reason") or "the build stream ended without a handoff")
            cost += float((failed or {}).get("cost_usd") or 0)
            ui.error(f"  chef failed: {ui.short(reason, 400)}")
            out = {"failed": reason[:2000], "cost_usd": round(cost, 4)}
            if gate.mode() == "auto":  # no Waiter, no extra paid call, no retry question
                return out
            d = waiter.diagnose(self.meter, self.run_grant, self.model, events, reason, body["task"], body["need"])
            if d["ok"]:
                out["diagnosis"] = d["cause"]
                ui.warn("  waiter: " + d["cause"].replace("\n", "\n          "))
            with_advice = " with this advice" if d["advice"] else ""
            if ck and ck["plan"] and ck.get("failed") and not d.get("replan"):  # good plan: keep it + the green tools
                ropts = [("resume", f"Resume from the checkpoint (plan + {len(ck['tools'])} green tools)"),
                         ("retry", "Retry from scratch" + with_advice)]
            else:
                ropts = [("retry", "Retry the Chef" + with_advice)]
            pick = gate.ask("Retry the Chef?", ropts + [("continue", "Continue without the tool")], auto="continue")
            if pick not in ("retry", "resume"):
                return out
            advice, resume = d["advice"], (ck["build_id"] if pick == "resume" else None)

    def _run_left(self, default: float | None = None) -> float:
        try:
            return float(self.meter.grant_info(self.run_grant)["left_usd"])
        except Exception:  # noqa: BLE001 - no number: the meter still enforces every cap on each call
            return float(self.caps.get("run") or 0) if default is None else default

    def _chef_stream(self, body: dict, opts: dict, label: str, count: bool):
        """One build: its own build_id and build grant (revoked on every path). Returns (handoff, failed, events,
        started, checkpoint {build_id, plan, tools}). A refused request (ShedError before the first event) is raised:
        it is not a Chef failure. Interactive mode shows the chef spinner while the stream runs."""
        build_id = new_id("b")
        g = self.meter.grant("build", parent=self.run_grant, cap_usd=opts["cap_usd"], label=label, build_id=build_id)
        body = {**body, "build_id": build_id, "grant": g,
                "options": {k: opts[k] for k in ("effort", "cap_seconds", "plan_seconds") if opts.get(k) is not None}}
        handoff = failed = stream = spent = None
        events: deque = deque(maxlen=waiter.KEEP_EVENTS)
        ck = {"build_id": build_id, "plan": False, "tools": [], "failed": False}
        chef, asked, pending, started = ui.ChefStatus(opts.get("cap_seconds") or 0), set(), False, False
        try:
            with chef:  # stopped on every exit path (Ctrl-C too) before the Waiter or the install gate
                stream = self.shed.chef_build(body)
                for ev in stream:
                    if not started:  # count a build only once shedd streams: a refused request (400) is free
                        started = True
                        self.builds += count
                    if not isinstance(ev, dict):
                        continue
                    events.append(ev)
                    chef.on(ev)
                    t = ev.get("type")
                    if t == "trace":
                        tool = f" {ev['tool']}" if ev.get("tool") else ""
                        ui.dim(f"  chef {ev.get('phase', '?')}{tool}: {ev.get('msg', '')} · "
                               f"${float(ev.get('cost_usd') or 0):.4f}")
                    elif t == "test_run":
                        self._show_test_run(ev)
                    elif t == "checkpoint":  # the plan JSON stays on the server (builds_log)
                        if ev.get("stage") == "plan":
                            ck["plan"] = True
                        elif ev.get("stage") == "tool" and ev.get("tool"):
                            ck["tools"] = list(dict.fromkeys([*ck["tools"], str(ev["tool"])]))
                            ui.dim(f"  chef: saved {ev['tool']} (green)")
                    elif t == "cap_hit" and (ev.get("kind"), ev.get("n")) not in asked:  # one prompt per event
                        asked.add((ev.get("kind"), ev.get("n")))
                        pending = True
                        if not self._cap_hit(ev, g, chef):
                            failed = {"reason": "a build cap was reached; the top-up answer did not reach the meter"}
                            break
                        pending = False
                    elif t == "handoff":
                        handoff = ev
                    elif t == "failed":  # a server-side failure: only then is the build resumable (status failed)
                        failed, ck["failed"] = ev, True
        except (ShedError, OSError, http.client.HTTPException) as e:
            if not started:
                raise ShedError(e.status, e.msg) if isinstance(e, ShedError) else ShedError(0, str(e)) from None
            failed = {"reason": f"the build stream broke: {type(e).__name__}: {ui.short(str(e), 200)}"}
        finally:
            if pending:  # Ctrl-C at a cap_hit: Stop (the server polls for the answer)
                with contextlib.suppress(Exception):
                    self.meter.topup(g, deny=True)
            with contextlib.suppress(Exception):  # the meter's figure is the authority (a broken stream has none)
                spent = float(self.meter.spent(g))
            self.meter.revoke(g)
            with contextlib.suppress(Exception):  # close the HTTP stream (a Ctrl-C leaves it open otherwise)
                getattr(stream, "close", lambda: None)()
        if started and handoff is None and failed is None:
            failed = {"reason": "the build stream ended without a handoff"}
        if failed is not None and spent is not None:
            failed = {**failed, "cost_usd": spent}
        return handoff, failed, list(events), started, ck

    def _cap_hit(self, ev: dict, g: str, chef) -> bool:
        """A build cap was hit: the operator raises it or stops the build (auto mode and Ctrl-C: Stop). The server
        polls the meter for the answer and does not count the wait. False = the answer did not reach the meter."""
        kind = "time" if ev.get("kind") == "time" else "usd"
        spent, cap, el, cap_s = (gate._num(ev.get(k), 0.0) for k in ("spent_usd", "cap_usd", "elapsed_s", "cap_s"))
        chef.sync(el, cap_s)
        if kind == "usd":  # never into the run reserve (the Waiter call and the answer); the meter clamps too
            room = round(self._run_left(0.0) - RUN_RESERVE_USD, 2)
            msg = f"Build cap reached (${spent:.2f} of ${cap:.2f}). Raise it?"
            opts = [(f"{x:.2f}", f"+${x:.2f}") for x in dict.fromkeys(min(x, room) for x in (0.5, 1.0)) if x >= 0.01]
        else:
            msg = f"Build time cap reached ({el / 60:.1f} of {cap_s / 60:g} min). Raise it?"
            opts = [("300", "+5 min"), ("600", "+10 min")]
        if gate.mode() == "auto" or not opts:
            ui.warn(f"  chef: {msg[:-10]}" + ("" if opts else " The run budget has no room for more."))
        pick = "stop"
        if opts:
            with chef.paused():
                pick = gate.ask(msg, opts + [("stop", "Stop the build")], auto="stop")
        try:
            if pick == "stop":
                d = self.meter.topup(g, deny=True)
            elif kind == "usd":
                d = self.meter.topup(g, usd=float(pick))
            else:
                d = self.meter.topup(g, seconds=int(pick))
                chef.cap_s += int(pick)
        except Exception as e:  # noqa: BLE001 - the build then ends (its grant is revoked), the run goes on
            ui.warn(f"  chef: the top-up answer did not reach the meter ({type(e).__name__}: {ui.short(str(e), 120)})")
            return False
        if pick != "stop":  # print what the meter applied (it clamps to the run / total room), not the pick
            d = d if isinstance(d, dict) else {"decision": "raised", "usd": float(pick) if kind == "usd" else 0}
            if d.get("decision") != "raised":
                ui.warn("  chef: the meter had no room for a raise; the build stops")
            else:
                usd = gate._num(d.get("usd"), 0.0)
                ui.info(f"  chef: cap raised (+{f'${usd:.2f}' if kind == 'usd' else f'{int(pick) // 60} min'})", "cyan")
        return True

    def _show_test_run(self, ev: dict) -> None:
        p, f = int(ev.get("passed") or 0), int(ev.get("failed") or 0)
        if ev.get("kind") == "stub_sanity":
            ui.dim(f"  chef P2 {ev.get('tool')}: stub check {p}/{p + f} pass (tests must fail on a stub)")
            return
        style = "green" if f == 0 and p > 0 else "yellow"
        ui.info(f"  chef test {ev.get('tool')} #{ev.get('iteration', '?')}: {p}/{p + f} passed", style)
        if f and ev.get("excerpt"):
            for ln in str(ev["excerpt"]).strip().splitlines()[:3]:
                ui.dim(f"      {ui.short(ln, 110)}")

    def _install(self, h: dict):
        pick = gate.install(self.shed, h)
        if pick == "reject":
            self.shed.reject(h["build_id"])
            ui.warn("  install rejected")
            return {"rejected": True, "note": "the operator rejected the install"}
        hashes = [
            r["content_hash"] for r in h.get("tree", []) if r.get("status") == "new" and r.get("content_hash")
        ]
        reg = self.shed.register(h["build_id"], hashes, pick).get("registered", [])
        for r in reg:
            self.built.add((r["name"], r["version"]))
        entry = next((r for r in reg if r["name"] == h.get("entry")), None)
        if entry:
            if pick == "always":
                self.approvals.allow_always(entry["perm_hash"], entry["name"], entry["version"])
            else:
                self.approvals.allow_once(entry["perm_hash"])
        # An improve that targets a sub-tool lists the entry as reused: its saving is counted at use, not here.
        reused = [r for r in h.get("tree", []) if r.get("status") == "reused" and r["name"] != h.get("entry")]
        if reused:
            tools = {t["name"]: t for t in self.shed.tools()}
            for r in reused:
                self._saving(r["name"], r.get("version"), (tools.get(r["name"]) or {}).get("build_cost_usd"))
        ui.ok("  installed: " + ", ".join(f"{r['name']} v{r['version']}" for r in reg))
        return {
            "installed": [f"{r['name']} v{r['version']}" for r in reg],
            "entry": h.get("entry"),
            "skill": h.get("skill"),
            "cost_usd": h.get("cost_usd"),
        }

    def _saving(self, name: str, version, build_cost) -> None:
        """Count a reuse once per session per (tool, version) that was not built in this session."""
        key = (name, version)
        if not build_cost or key in self.built or key in self.saved:
            return
        self.saved.add(key)
        self.saved_usd += float(build_cost)
        self.meter.record_saving(None, name, version, float(build_cost))
        ui.dim(f"  reuse {name} v{version}: saved ${float(build_cost):.4f} build cost")
