"""Big Chef (contract section 8): P1 plan -> P2 tests -> P3 code -> P4 security per new tool -> P5 handoff.

P1 (the pro planner) runs first; in improve mode it also picks the target (the entry or a sub-tool). Then the deps
install serially, and a pool of 4 threads builds every new small tool and the entry at the same time: P2-P4 with
mocks need only the specs. The entry's live smoke run waits for the small tools. Every role gets a fresh conversation
that starts with one byte-stable prefix (prompts/), so the provider caches it. Every LLM call goes through the meter
(shed.llm.chat). DeepSeek thinking mode stays on: no forced tool_choice, and each assistant message keeps its
reasoning_content (conversations are append-only, also when the coder escalates to the pro model).
"""
from __future__ import annotations

import ast
import json
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from shed import llm, lookup, manifest, pkgindex, runner
from shed.chef import static_check

# role -> (model, reasoning_effort); env CHEF_<ROLE>_MODEL / CHEF_<ROLE>_EFFORT. "escalate" = the coder of a tool
# after its first red test run or smoke failure.
_ROLE_DEFAULT = {"plan": ("deepseek-v4-pro", "high"), "tests": ("deepseek-flash", "low"),
                 "code": ("deepseek-flash", "high"), "security": ("deepseek-flash", "high"),
                 "escalate": ("deepseek-v4-pro", "high")}
ROLE = {r: (os.environ.get(f"CHEF_{r.upper()}_MODEL") or m, os.environ.get(f"CHEF_{r.upper()}_EFFORT") or e)
        for r, (m, e) in _ROLE_DEFAULT.items()}
MAX_TOKENS = {"plan": 32768, "tests": 12288, "code": 32768, "security": 6144}
CAP_NEW_SMALL = 3          # new or extended small tools per build (+ the entry tool)
CAP_ITER = 4               # coder iterations per tool
CAP_ITER_BONUS = 1         # one more iteration after a security reject
CAP_TURNS = 48             # LLM turns per build
CAP_SECONDS = 6 * 60       # wall time per build
PLAN_SECONDS = 150         # after this, the planner gets its last turn
PLAN_TURNS = 8
PLAN_FIX_TURNS = 2         # extra P1 turns to fix a rejected plan (once)
PLAN_PROBES = 9            # source probes in P1 (up to 3 per turn, in parallel)
CAP_PROBES = 8             # probe/request_package calls per tool
CUT_RETRIES = 2            # replies cut at the token limit that do not count as an iteration
SMOKE_FIXES = 1            # coder rounds driven by the entry's live smoke run
WORKERS = 4                # CAP_NEW_SMALL + the entry: every job runs at once, so the smoke wait cannot deadlock
FEEDBACK_CHARS = 4096
SKILL_CHARS = 1200
SAMPLE_CHARS = 1500
MANIFEST_KEYS = ("name", "parent", "grade", "summary", "description", "keywords", "input_schema",
                 "output_schema", "uses", "deps", "permissions", "limits", "examples")
DEFAULT_PERMS = {"network": False, "llm_usd": 0, "files": "none"}
CUT = "Cut at the token limit: send the full tool.py now, no analysis."
STUB = ("def run(args, shed):\n    raise NotImplementedError\n\n\n"
        "def http_get(*a, **k):\n    raise NotImplementedError\n\n\n"
        "def http_post(*a, **k):\n    raise NotImplementedError\n")

PROMPTS = Path(__file__).with_name("prompts")


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


PREFIX = "\n\n".join(_prompt(n) for n in ("rules", "manifest", "sdk_cheatsheet", "checklist"))
PROMPT = {r: _prompt(r) for r in ("plan", "tests", "code", "security")}


def _fn(name: str, desc: str, props: dict, required: list | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(props) if required is None else required}}}


S = {"type": "string"}
PLAN_TOOLS = [
    _fn("explore", "Search the tool registry (5 rows per page).", {"query": S, "page": {"type": "integer"}}, ["query"]),
    _fn("pkg_search", "Search the package catalog (8 hits).", {"query": S}),
    _fn("read_tool_skill", "Read one tool's SKILL.md and schemas.", {"name": S}),
    _fn("probe", "Run Python in the sandbox (30 s, network on) to check that a source answers and to see its "
        "real shape. Print the status, the final URL and one real item (<= 1500 chars) or the JSON path to the "
        "items.", {"code": S}),
    _fn("submit_plan", "Submit the final plan and the real data samples.",
        {"plan": {"type": "object"}, "samples": {"type": "object"}}, ["plan"]),
]
CODE_TOOLS = [
    _fn("probe", "Run Python in the sandbox (30 s, network on, tool.py importable). Returns an excerpt.",
        {"code": S}),
    _fn("request_package", "Install a catalog package and add it to deps.", {"name": S}),
]

FENCE = re.compile(r"```([\w+-]*)[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
SEARCH_REPLACE = re.compile(r"^<<<<<<< SEARCH[ \t]*\n(.*?)^=======[ \t]*\n(.*?)^>>>>>>> REPLACE", re.DOTALL | re.MULTILINE)
DISPUTE = re.compile(r"^\s*DISPUTE:\s*(.+)", re.DOTALL)
LIVE = re.compile(r"(@|\bmark\.)live\b")


@dataclass
class BuildCtx:
    db: object
    grant: str
    task: str
    need: str
    lookup_id: str | None
    session_id: str
    build_id: str
    repair_of: dict | None = None  # {tool, invoke_id, problem?, args?, error?}: repair or improve that tool
    chain: object | None = None    # shed.chain.Chain for the smoke run; None = no smoke run


class BuildFailed(Exception):
    pass


# --- message helpers (mirror contract section 3) -------------------------------------------------------------
def _assistant(resp: dict) -> dict:
    m = resp["choices"][0]["message"]
    out = {"role": "assistant", "content": m.get("content") or ""}
    if m.get("reasoning_content"):
        out["reasoning_content"] = m["reasoning_content"]
    if m.get("tool_calls"):
        out["tool_calls"] = m["tool_calls"]
    return out


def _calls(resp: dict) -> list[tuple[str, str, dict | str]]:
    out = []
    for c in resp["choices"][0]["message"].get("tool_calls") or []:
        raw = c["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = raw
        out.append((c["id"], c["function"]["name"], args))
    return out


def _text(resp: dict) -> str:
    return resp["choices"][0]["message"].get("content") or ""


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _sys(role: str) -> dict:
    return {"role": "system", "content": f"{PREFIX}\n\n{PROMPT[role]}"}


def _js(x) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def _json_obj(text: str) -> dict | None:
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        v = json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def _block(text: str, langs: tuple[str, ...], must: str = "") -> str | None:
    for lang, body in FENCE.findall(text):
        if lang.lower().rstrip("0123456789") in langs and must in body:
            return body
    return None


def _excerpt(log: str, limit: int = FEEDBACK_CHARS) -> str:
    marks = [i for i in (log.find("= ERRORS ="), log.find("= FAILURES =")) if i >= 0]
    if marks:
        log = log[log.rfind("\n", 0, min(marks)) + 1:]
    if len(log) > limit:
        log = log[:limit - 900] + "\n[...]\n" + log[-800:]
    return log


def _tail(log: str, n: int = 400) -> str:
    return log.strip()[-n:]


def _green(r) -> bool:
    return bool(r.passed) and not (r.failed or r.errors or r.timed_out)


def _apply(reply: str, code: str) -> tuple[str | None, str | None]:
    """Returns (new tool.py or None, error). SEARCH/REPLACE blocks win over a full file."""
    edits = SEARCH_REPLACE.findall(reply)
    if edits:
        for k, (old, new) in enumerate(edits, 1):
            if old in code:
                code = code.replace(old, new, 1)
                continue
            loose = "\n".join(line.rstrip() for line in old.splitlines())
            norm = "\n".join(line.rstrip() for line in code.splitlines()) + "\n"
            if loose and loose in norm:
                code = norm.replace(loose, "\n".join(line.rstrip() for line in new.splitlines()), 1)
                continue
            return None, f"SEARCH block {k} does not match tool.py:\n{old[:300]}\nResend it, or send the full file."
        return code, None
    full = _block(reply, ("python", "py", ""), "def run(")
    if full:
        return full, None
    return None, "No tool.py found. Reply with SEARCH/REPLACE blocks or the full tool.py in a ```python block."


def _skill_fallback(m: dict) -> str:
    args = ", ".join((m.get("input_schema") or {}).get("properties") or {}) or "see input_schema"
    p, lim = m.get("permissions") or {}, m.get("limits") or {}
    return (f"# {m['name']}\n{m.get('summary', '')}\n\nWhen to use: {m.get('description', '')[:500]}\n"
            f"Args: {args}\nOutput: {_js(m.get('output_schema') or {})[:300]}\n"
            f"Limits: timeout {lim.get('timeout_s', 60)} s; network {p.get('network')}; "
            f"LLM <= ${p.get('llm_usd', 0)}.\n")[:SKILL_CHARS]


def _norm_spec(spec: dict, entry: bool = False) -> dict:
    """Fix trivia (summary length, keyword count, missing permissions/limits, entry grade); keep semantics."""
    m = {k: v for k, v in spec.items() if k in MANIFEST_KEYS and k != "parent"}
    for k, v in (("keywords", []), ("uses", []), ("deps", [])):
        m.setdefault(k, v)
    m.setdefault("description", m.get("summary", ""))
    s = " ".join(str(m.get("summary") or m.get("description") or m.get("name", "")).split())
    m["summary"] = s if len(s) <= 120 else s[:117].rstrip() + "..."
    if isinstance(m["keywords"], str):
        m["keywords"] = [k.strip() for k in m["keywords"].split(",") if k.strip()]
    if isinstance(m["keywords"], list):
        m["keywords"] = m["keywords"][:16]
    p = m.get("permissions")
    if p is None or isinstance(p, dict):
        m["permissions"] = {k: (p or {}).get(k, v) for k, v in DEFAULT_PERMS.items()}
    lim = m.get("limits")
    if lim is None or isinstance(lim, dict):
        lim = {"timeout_s": 60, **{k: v for k, v in (lim or {}).items() if k in ("timeout_s", "memory_mb")}}
        bounds = {"timeout_s": (1, 180), "memory_mb": (128, 4096)}
        m["limits"] = {k: max(bounds[k][0], min(v, bounds[k][1])) if type(v) is int else v for k, v in lim.items()}
    if entry:
        m["grade"] = "big" if m.get("uses") else "small"
    return m


def _norm_samples(s) -> dict[str, str]:
    if isinstance(s, (str, list)):
        s = {"source": s}
    if not isinstance(s, dict):
        return {}
    return {str(k)[:200]: (v if isinstance(v, str) else _js(v))[:SAMPLE_CHARS] for k, v in list(s.items())[:4] if v}


def _words(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " " + " ".join(re.findall(r"[a-z0-9]+", s)) + " "


def _task_defaults(m: dict, task: str) -> list[str]:
    """Input properties whose default holds a value from the task text (a string >= 3 chars, a number >= 4 digits)."""
    t, bad = _words(task), []
    for k, p in ((m.get("input_schema") or {}).get("properties") or {}).items():
        d = p.get("default") if isinstance(p, dict) else None
        for v in d if isinstance(d, list) else [d]:
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                w = _words(str(v))
                if len(w.strip()) >= (3 if isinstance(v, str) else 4) and w in t:
                    bad.append(k)
                    break
    return bad


def _compat(old: dict, new: dict) -> list[str]:
    oi, ni = old.get("input_schema") or {}, new.get("input_schema") or {}
    lost = sorted(set(oi.get("properties") or {}) - set(ni.get("properties") or {}))
    added = sorted(set(ni.get("required") or []) - set(oi.get("required") or []))
    return ([f"the extend drops input properties {lost}; keep every old one"] if lost else []) + (
        [f"the extend makes {added} required; new inputs must be optional"] if added else [])


def _top_names(code: str) -> set[str]:
    out = set()
    for n in ast.parse(code).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        for t in n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def join_tests(base: str, extra: str, tag: str) -> str:
    """Append the additional tests to the old test_tool.py; rename clashing top-level names in the new part, so the
    old tests (and their helpers and fixtures) stay as they are."""
    extra = re.sub(r"^from __future__ import .*$", "", extra, flags=re.MULTILINE)
    try:
        taken = _top_names(base)
        clash = _top_names(extra) & taken
    except SyntaxError:
        clash, taken = set(), set()
    for n in sorted(clash, key=len, reverse=True):
        alt = n + tag
        while alt in taken:
            alt += "_"
        extra = re.sub(rf"\b{re.escape(n)}\b", alt, extra)
    return f"{base.rstrip()}\n\n\n# --- added in {tag.lstrip('_')} ---\n{extra.strip()}\n"


def _parse_ref(ref: str) -> tuple[str, int | None]:
    name, _, v = str(ref).partition("@")
    v = v.lstrip("v")
    return name.strip(), int(v) if v.isdigit() else None


def smoke_problems(r: dict) -> tuple[list[str], str]:
    """Generic checks on a live run: an error, no results, a field empty in every result, refused subcalls."""
    if not r.get("ok"):
        return [f"the run failed: {str(r.get('error'))[:600]}\n{str(r.get('traceback') or '')[-800:]}"], ""
    res, out = r.get("result"), []
    if r.get("refused_subcalls"):
        out.append(f"{r['refused_subcalls']} shed.call subcalls were refused by the chain limits (<= 20 per run): batch them")
    rows = res.get("results") if isinstance(res, dict) else None
    if rows is None and isinstance(res, dict):
        rows = next((v for v in res.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
    warns = [str(w)[:200] for w in (res.get("warnings") or [])][:4] if isinstance(res, dict) else []
    if isinstance(res, dict) and "results" in res and not rows:
        out.append("0 results for the example args" + (f" (warnings: {warns})" if warns else ""))
    items = [x for x in rows or [] if isinstance(x, dict)]
    if len(items) >= 2:
        keys = {k for x in items for k in x}
        empty = sorted(k for k in keys if all(x.get(k) in (None, "", [], {}) for x in items))
        if empty:
            out.append(f"fields empty in all {len(items)} results: {', '.join(empty)}"
                       + (f" (warnings: {warns})" if warns else ""))
    return out, (_js(items[0])[:700] if items else _js(res)[:700])


# --- the Chef ------------------------------------------------------------------------------------------------
class Chef:
    def __init__(self, ctx: BuildCtx, emit: Callable[[dict], None]):
        self.ctx, self.db, self._emit = ctx, ctx.db, emit
        self.cost, self.turns, self.t0 = 0.0, 0, time.monotonic()
        self.lock, self.pkg_lock, self.stop = threading.RLock(), threading.Lock(), threading.Event()
        self.tool_cost: dict[str, float] = {}
        self.warnings: list[str] = []
        self.reuse: dict[str, dict] = {}     # name -> db version row
        self.built: dict[str, dict] = {}     # name -> {manifest, files, content_hash, perm_hash, tests, iterations}
        self.specs: dict[str, dict] = {}     # name -> planned manifest of every tool this build makes (plan order)
        self.old: dict[str, dict] = {}       # name -> the version row an "extend" starts from
        self.samples: dict[str, str] = {}    # source -> a real item copied from a P1 probe
        self.small_futs: list = []
        self.failure, self.fail_args = "", None   # improve mode: problem/error/args (+ the planner's diagnosis)

    # events, caps, shared state
    def emit(self, ev: dict) -> None:
        with self.lock:
            self._emit(ev)  # the caller's emit streams the line and stores it (db.add_trace)

    def trace(self, phase: str, tool: str | None, msg: str) -> None:
        with self.lock:
            self.emit({"type": "trace", "phase": phase, "tool": tool, "msg": msg, "cost_usd": round(self.cost, 4)})

    def warn(self, msg: str) -> None:
        with self.lock:
            self.warnings.append(msg)

    def check_time(self) -> None:
        if time.monotonic() - self.t0 > CAP_SECONDS:
            raise BuildFailed(f"cap: {CAP_SECONDS // 60} min wall time")

    def chat(self, role: str, messages: list, *, tools: list | None = None, tool: str | None = None,
             pro: bool = False) -> dict:
        if self.stop.is_set():
            raise BuildFailed("stopped: another part of the build failed")
        with self.lock:
            if self.turns >= CAP_TURNS:
                raise BuildFailed(f"cap: {CAP_TURNS} LLM turns")
            self.turns += 1
        self.check_time()
        model, effort = ROLE["escalate" if pro else role]
        body = {"model": model, "messages": messages, "reasoning_effort": effort, "max_tokens": MAX_TOKENS[role]}
        if tools:
            body["tools"] = tools
        resp = llm.chat(self.ctx.grant, body, role, tool=tool)
        c = float((resp.get("x_meter") or {}).get("cost_usd") or 0.0)
        with self.lock:
            self.cost += c
            key = tool or "_plan"
            self.tool_cost[key] = self.tool_cost.get(key, 0.0) + c
        messages.append(_assistant(resp))
        return resp

    # registry helpers
    def version(self, name: str, v: int | None = None) -> dict | None:
        row = self.db.get_version(name, v)
        return row if row and row.get("files") is not None else None

    def interfaces(self, uses: list[str]) -> str:
        out = []
        for u in uses:
            spec = self.specs.get(u)    # a tool of this build: its spec (it is built at the same time)
            b = None if spec else (self.reuse.get(u) or self.version(u))
            m = spec or (b or {}).get("manifest")
            if not m:
                out.append(f"### {u}\n(unknown)")
                continue
            doc = ("Spec:\n" + m.get("description", "")) if spec else ("SKILL.md:\n" + b["files"].get("SKILL.md", ""))
            out.append(f"### {u} ({m.get('grade')})\n{m.get('summary', '')}\ninput_schema: {_js(m.get('input_schema'))}\n"
                       f"output_schema: {_js(m.get('output_schema'))}\n{doc[:800]}")
        return "\n\n".join(out) or "(none)"

    def sample_text(self) -> str:
        if not self.samples:
            return ""
        return ("\n\nReal data samples (copied from probes; build fixtures and parsers on these shapes):\n"
                + "\n".join(f"### {k}\n{v}" for k, v in self.samples.items()))

    # P1 plan
    def check_plan(self, plan, samples=None) -> tuple[list[str], dict | None]:
        if isinstance(plan, str):
            plan = _json_obj(plan)
        if isinstance(plan, dict) and "entry" not in plan and isinstance(plan.get("plan"), dict):
            plan, samples = plan["plan"], samples or plan.get("samples")
        if not isinstance(plan, dict) or not isinstance(plan.get("entry"), dict):
            return ["the plan needs \"entry\" (a manifest) and \"small\" (a list)"], None
        rep = (self.ctx.repair_of or {}).get("tool")
        errs, reuse, new, old = [], {}, [], {}
        active = {t["name"] for t in self.db.active_tools()}

        def spec(item: dict, entry: bool) -> dict | None:
            if "extend" not in item:
                return _norm_spec(item if entry else {"grade": "small", **item}, entry)
            name, _ = _parse_ref(item["extend"])
            row = self.version(name)
            if not row or name in old:
                errs.append(f"extend {item['extend']}: " + ("extended twice" if row else "no such tool"))
                return None
            if item.get("name", name) != name:
                errs.append(f"extend {name}: an extend keeps the name")
            base = {k: v for k, v in row["manifest"].items() if k in MANIFEST_KEYS}
            m = _norm_spec({**base, **{k: v for k, v in item.items() if k != "extend"}, "name": name}, entry)
            m["parent"] = f"{name}@v{row['version']}"
            errs.extend(f"{name}: {e}" for e in _compat(row["manifest"], m))
            old[name] = row
            return m

        e, entry_row = plan["entry"], None
        if "reuse" in e:
            name, _ = _parse_ref(e["reuse"])
            entry_row = self.version(name) if rep and name == rep else None
            if not entry_row:
                return [f"improve mode: the entry is {rep}; extend it or reuse it" if rep else
                        "the entry is a new tool or an extend of a registry tool, not a reuse"], None
            entry = entry_row["manifest"]
        else:
            entry = spec(e, True)
            if entry is None:
                return errs, None
        for item in plan.get("small") or []:
            if isinstance(item, dict) and "reuse" in item:
                if not item["reuse"]:
                    continue  # {"reuse": null}: the planner's placeholder for "nothing to reuse"
                name, v = _parse_ref(item["reuse"])
                row = self.version(name, v)
                if row:
                    reuse[name] = row
                else:
                    errs.append(f"reuse {item['reuse']}: no such tool")
            elif isinstance(item, dict):
                m = spec(item, False)
                if m:
                    new.append(m)
            else:
                errs.append("each small item is {\"reuse\": \"name@vN\"}, {\"extend\": \"name@vN\", ...} or a manifest")
        if len(new) > CAP_NEW_SMALL:
            errs.append(f"{len(new)} new or extended small tools; the cap is {CAP_NEW_SMALL}")
        if rep and not entry_row and entry.get("name") != rep:
            errs.append(f"improve mode: the entry is {rep}; extend it or reuse it")
        if rep and entry.get("name") == rep and not entry_row and isinstance(self.fail_args, dict):
            entry["examples"] = [{"args": self.fail_args, "note": "the call with the problem"}] + [
                x for x in entry.get("examples") or [] if not (isinstance(x, dict) and x.get("args") == self.fail_args)]
        jobs = new + ([] if entry_row else [entry])
        planned = [m.get("name") for m in jobs]
        if len(set(planned)) < len(planned):
            errs.append("a tool name appears twice in the plan")
        for m in jobs + ([entry] if entry_row else []):
            n = m.get("name", "?")
            for u in m.get("uses") or []:
                if u not in planned and u not in reuse:
                    row = self.version(u) if u in active else None
                    if row:
                        reuse[u] = row
                    else:
                        errs.append(f"{n}: uses {u!r}, which is not a registry tool or a tool of this plan")
            if m is entry and entry_row:
                continue
            errs += [f"{n}: {x}" for x in manifest.validate(m)]
            if n not in old and (n in active or n in reuse):
                errs.append(f"{n}: the name exists; reuse or extend that tool, or pick a new name")
            errs += [f"{n}: dep {d!r} is not in the catalog" for d in m.get("deps") or [] if not pkgindex.in_catalog(d)]
            errs += [f"{n}: the default of {k!r} is a value from the task; task values are args, not defaults"
                     for k in _task_defaults(m, self.ctx.task)]
        for m in new:
            if m.get("grade") != "small":
                errs.append(f"{m.get('name')}: new tools in \"small\" need grade small")
        used = {u for m in new + [entry] for u in m.get("uses") or []}
        errs += [f"{m.get('name')}: no tool uses it; drop it or add it to a uses list" for m in new
                 if m.get("name") not in used]
        if entry.get("grade") == "small" and new:
            errs.append("a small entry stands alone: put new small tools only under a big entry")
        net = any((m.get("permissions") or {}).get("network") for m in [entry, *new, *(r["manifest"] for r in reuse.values())])
        if not entry_row and entry.get("grade") == "small" and (entry.get("permissions") or {}).get("network") \
                and not active:
            errs.append(f"{entry.get('name')}: the registry is empty, so split the work into generic small tools "
                        "(fetch, extract, normalise) under a big entry")
        if rep and not set(old) & {rep, *(entry.get("uses") or [])}:
            errs.append(f"improve mode: extend {rep} or one of its sub-tools")
        samples = _norm_samples(samples if samples is not None else plan.get("samples"))
        if net and not samples:
            errs.append("samples: a networked plan needs samples {\"<source>\": \"<= 1500 chars of a REAL item "
                        "copied from a probe\"}")
        return errs, {"entry": entry, "new": new, "reuse": reuse, "old": old, "entry_row": entry_row,
                      "samples": samples, "notes": str(plan.get("notes") or "")[:600]}

    def improve_request(self) -> str:
        rep = self.ctx.repair_of or {}
        row = self.version(rep.get("tool") or "")
        if not row:
            raise BuildFailed(f"improve: no active tool {rep.get('tool')!r}")
        inv = self.db.get_invocation(rep.get("invoke_id") or "") or {}
        err, problem, args = rep.get("error") or inv.get("error"), rep.get("problem"), rep.get("args")
        self.fail_args = args if isinstance(args, dict) else None
        self.failure = "\n".join(x for x in (
            f"Problem (seen by the agent): {str(problem)[:1500]}" if problem else "",
            f"Error: {str(err)[:1500]}" if err else "",
            f"Args: {_js(args)[:1500]}" if self.fail_args is not None else "") if x) or "(no details)"
        ref = f"{row['name']}@v{row['version']}"
        self.trace("P1", row["name"], f"improve {ref}: {str(problem or err or '')[:100]}")
        m = {k: v for k, v in row["manifest"].items() if k != "version"}
        return (f"Improve {ref}.\nTask: {self.ctx.task}\nNeed: {self.ctx.need}\n{self.failure}\n\n"
                f"manifest.json:\n{_js(m)}\n\nSKILL.md:\n{row['files'].get('SKILL.md', '')}\n\n"
                f"tool.py:\n```python\n{row['files'].get('tool.py', '')}\n```\n\n"
                f"Sub-tools (uses):\n{self.interfaces(m.get('uses') or [])}")

    def plan_probe(self, args) -> str:
        self.check_time()
        r = runner.probe(str((args if isinstance(args, dict) else {}).get("code", "")), None, timeout_s=30)
        first = (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or [""])[0]
        self.trace("P1", None, f"probe source: exit {r.exit}" + (" (timeout)" if r.timed_out else f" · {first[:90]}"))
        return f"exit={r.exit}{' TIMEOUT' if r.timed_out else ''}\nstdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-800:]}"

    def plan(self) -> dict:
        ctx = self.ctx
        if ctx.repair_of:
            first = self.improve_request()
        else:
            rows = []
            if ctx.lookup_id:
                rows = (self.db.get_lookup(ctx.lookup_id) or {}).get("rows") or []
                rows = json.loads(rows) if isinstance(rows, str) else rows
            n = len(self.db.active_tools())
            reg = (f"Registry: {n} active tools." if n else
                   "Registry: EMPTY (0 tools). Do not call explore or read_tool_skill; probe sources and plan new tools.")
            first = f"Task: {ctx.task}\nNeed: {ctx.need}\nLookup top-3: {_js(rows[:3])}\n{reg}"
            self.trace("P1", None, "plan: reading the task and the registry")
        msgs = [_sys("plan"), _user(first)]
        probes, limit, turn, fixed, late = 0, PLAN_TURNS, -1, False, False
        while (turn := turn + 1) < limit:
            if not late and time.monotonic() - self.t0 > PLAN_SECONDS:
                late, limit = True, min(limit, turn + 1)
            if turn == limit - 1:
                msgs.append(_user("Last turn: call submit_plan now."))
            resp = self.chat("plan", msgs, tools=PLAN_TOOLS)
            calls = _calls(resp)
            if not calls:
                errs, plan = self.check_plan(_json_obj(_text(resp)))
                if plan and not errs:
                    return plan
                msgs.append(_user("Call submit_plan(plan, samples).\nProblems:\n- " + "\n- ".join(errs)))
                continue
            pc = [(cid, a) for cid, name, a in calls if name == "probe"]   # one turn's probes run in parallel
            room = max(0, PLAN_PROBES - probes)
            probes += len(pc)
            with ThreadPoolExecutor(3) as ex:
                outs = dict(zip([c for c, _ in pc[:room]], ex.map(self.plan_probe, [a for _, a in pc[:room]])))
            done = None
            for cid, name, args in calls:
                args = args if isinstance(args, dict) else {}
                if name == "submit_plan":
                    errs, plan = self.check_plan(args.get("plan", args if "entry" in args else None), args.get("samples"))
                    if plan and not errs:
                        done, out = plan, "accepted"
                    else:
                        out = "Plan rejected:\n- " + "\n- ".join(errs) + "\nFix only these problems and resubmit."
                        self.trace("P1", None, f"plan rejected: {errs[0][:120]}")
                        if not fixed:  # one fix window, so a late rejection is not fatal
                            fixed, limit = True, max(limit, turn + 1 + PLAN_FIX_TURNS)
                elif name == "explore":
                    hits = lookup.explore(self.db, str(args.get("query", "")), int(args.get("page") or 0), k=5)
                    out = _js(hits)
                    self.trace("P1", None, f"explore {args.get('query')!r}: {len(hits)} hits")
                elif name == "pkg_search":
                    hits = pkgindex.search(str(args.get("query", "")), k=8)
                    out = _js(hits)
                    self.trace("P1", None, f"pkg_search {args.get('query')!r}: {len(hits)} hits")
                elif name == "read_tool_skill":
                    row = self.version(*_parse_ref(args.get("name", "")))
                    info = {"name": row["name"], "version": row["version"], "skill": row["files"].get("SKILL.md"),
                            **{k: row["manifest"].get(k) for k in ("grade", "uses", "input_schema", "output_schema")}
                            } if row else None
                    if info and ctx.repair_of:
                        info["tool.py"] = row["files"].get("tool.py", "")[:12000]
                    out = _js(info) if info else "no such tool"
                    self.trace("P1", None, f"read {args.get('name')}")
                elif name == "probe":
                    out = outs.get(cid) or f"probe budget used ({PLAN_PROBES}); submit the plan with the sources you verified"
                else:
                    out = f"unknown tool {name}"
                msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
            if done:
                return done
        raise BuildFailed(f"plan: no valid plan in {limit} turns")

    # P2 tests
    def write_tests(self, m: dict, ifaces: str, failure: str, old: dict | None) -> tuple[str, list]:
        name = m["name"]
        req = f"Spec (manifest.json):\n{_js(m)}\n\nInterfaces of uses:\n{ifaces}{self.sample_text()}"
        if old:
            req += (f"\n\nThis is a new version of {m.get('parent')}."
                    + (f" It failed or gave a bad result in use:\n{failure}\n" if failure else "\n")
                    + f"Current test_tool.py (it stays as it is):\n```python\n{old['files'].get('test_tool.py', '')}\n```\n"
                    "Write ADDITIONAL tests only, for the spec changes"
                    + (" and a regression test for this problem" if failure else "")
                    + ". They are appended to the current file. Reply with one ```python block: the new tests and "
                      "their imports.")
        msgs = [_sys("tests"), _user(req)]
        self.trace("P2", name, "tests: writing additional tests" if old else "tests: writing from the spec")
        tests = self.ask_tests(msgs, name)
        for attempt in range(2):
            self.check_time()
            r, _ = self.run_tests(m, {"tool.py": STUB, "test_tool.py": tests, "SKILL.md": ""}, 0, "stub_sanity")
            fails = r.failed + r.errors + int(r.timed_out)
            if r.failed >= 1:
                self.trace("P2", name, f"tests: {r.passed + fails} collected, stub fails {fails} (good)")
                break
            if attempt:
                self.warn(f"{name}: no test fails on a stub")
                break
            msgs.append(_user(f"On a stub whose run() raises NotImplementedError: passed={r.passed}, "
                              f"failed={r.failed}, errors={r.errors}. At least one test must fail on the stub and all "
                              f"tests must be collected.\n{_excerpt(r.log, 1500)}\n"
                              + ("Reply with the additional tests." if old else "Reply with the full test_tool.py.")))
            self.trace("P2", name, "tests: stub sanity failed, regenerating once")
            tests = self.ask_tests(msgs, name)
        return tests, msgs

    def ask_tests(self, msgs: list, name: str) -> str:
        reply = _text(self.chat("tests", msgs, tool=name))
        tests = _block(reply, ("python", "py", ""), "def test") or (reply if "def test" in reply else "")
        if not tests:
            raise BuildFailed(f"{name}: the test writer gave no test_tool.py")
        return self.joined(name, tests)

    def joined(self, name: str, tests: str) -> str:
        """For an extend: the old test_tool.py + the additional tests (regression tests stay by construction)."""
        old = self.old.get(name)
        return join_tests(old["files"].get("test_tool.py", ""), tests, f"_v{old['version'] + 1}") if old else tests

    def run_tests(self, m: dict, files: dict, it: int, kind: str = "tests", live: bool = False):
        """Coder iterations run without @live tests (recorded as "offline" when the file has some); the final run
        with live=True is the recorded passing "tests" run."""
        r = runner.run_tests({"tool.py": files["tool.py"], "test_tool.py": files["test_tool.py"]}, live=live)
        fails = r.failed + r.errors + int(r.timed_out)
        if kind == "tests" and not live and LIVE.search(files["test_tool.py"]):
            kind = "offline"
        h = self.db.save_draft(self.ctx.build_id, m, dict(files), round(self.tool_cost.get(m["name"], 0.0), 6))
        self.db.record_test_run(h["content_hash"], kind, r.passed, fails, r.log)
        self.emit({"type": "test_run", "tool": m["name"], "kind": "stub_sanity" if kind == "stub_sanity" else "tests",
                   "live": live, "passed": r.passed, "failed": fails, "iteration": it, "excerpt": _tail(r.log)})
        return r, h

    # P3 code (+ P4 after each green run)
    def code_tool(self, m: dict, files: dict, ifaces: str, tmsgs: list, failure: str, old: dict | None,
                  smoke: bool = False) -> dict:
        name = m["name"]
        req = (f"Spec (manifest.json):\n{_js(m)}\n\nInterfaces of uses:\n{ifaces}{self.sample_text()}\n\n"
               f"test_tool.py:\n```python\n{files['test_tool.py']}\n```")
        if old:
            r, _ = self.run_tests(m, files, 0)
            state = (f"Tests on the current code: {r.passed} passed, {r.failed + r.errors} failed.\n{_excerpt(r.log)}"
                     if not _green(r) else "All tests pass on the current code" + (
                         ", but the tool failed in use. Fix the cause." if failure else ". Still meet the new spec."))
            req += (f"\n\nThis is a new version of {m.get('parent')}. Change the current code to meet the spec."
                    + (f"\nIt failed or gave a bad result in use:\n{failure}" if failure else "")
                    + f"\n\nCurrent tool.py:\n```python\n{files['tool.py']}\n```\n\nCurrent SKILL.md:\n```markdown\n"
                      f"{files['SKILL.md']}\n```\n\n{state}\nReply with SEARCH/REPLACE blocks or the full tool.py.")
        else:
            req += "\n\nWrite tool.py and SKILL.md."
        msgs = [_sys("code"), _user(req)]
        it, max_it, disputed, sec_retry, probes, announce, cut, pro = 0, CAP_ITER, False, False, 0, True, 0, False
        smoke_fixes = SMOKE_FIXES if smoke else 0
        while True:
            if announce:
                self.trace("P3", name, f"code: iteration {it + 1} of {max_it}" + (" (pro)" if pro else ""))
            resp = self.chat("code", msgs, tools=CODE_TOOLS, tool=name, pro=pro)
            calls = _calls(resp)
            announce = not calls
            if calls:
                for cid, fn, args in calls:
                    probes += 1
                    out = (f"budget used ({CAP_PROBES} calls); write the code now" if probes > CAP_PROBES
                           else self.code_tool_call(m, files, fn, args if isinstance(args, dict) else {}))
                    msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
                continue
            reply = _text(resp)
            if (resp["choices"][0].get("finish_reason") == "length" and cut < CUT_RETRIES
                    and not _block(reply, ("python", "py", ""), "def run(")):
                cut, announce = cut + 1, False  # a cut reply is not an iteration
                self.trace("P3", name, "code: reply cut at the token limit; asking for the full tool.py")
                msgs.append(_user(CUT))
                continue
            d = DISPUTE.match(reply)
            if d and not disputed:
                disputed = True
                msgs.append(_user(self.dispute(m, files, tmsgs, d.group(1).strip())))
                continue
            code, err = _apply(reply, files["tool.py"])
            skill = _block(reply, ("markdown", "md"))
            if skill:
                files["SKILL.md"] = skill.strip()[:SKILL_CHARS]
            it += 1
            if err:
                feedback = err
                self.trace("P3", name, f"code: iteration {it} gave no usable tool.py ({err.splitlines()[0][:80]})")
            else:
                files["tool.py"] = code
                files["SKILL.md"] = files["SKILL.md"] or _skill_fallback(m)
                self.check_time()
                r, h = self.run_tests(m, files, it)
                report = ""
                if _green(r):
                    self.trace("P3", name, f"tests {r.passed}/{r.passed} pass at iteration {it}")
                    problems, report = self.smoke(m, files) if smoke else ([], "")
                    if problems and smoke_fixes and it < max_it:
                        smoke_fixes -= 1
                    elif problems:
                        self.warn(f"{name}: smoke run on real data: {'; '.join(problems)[:300]}")
                        report = ""
                    if not report and LIVE.search(files["test_tool.py"]):
                        r, h = self.run_tests(m, files, it, live=True)   # the final, recorded run
                        if not _green(r):
                            self.trace("P3", name, f"live tests: {r.passed}/{r.passed + r.failed + r.errors} pass")
                if report:
                    feedback, pro = report, True
                elif _green(r):
                    verdict, reasons = self.review(m, files, h["content_hash"], r)
                    if verdict == "approve":
                        return {"manifest": m, "files": files, "content_hash": h["content_hash"],
                                "perm_hash": h["perm_hash"], "tests": f"{r.passed}/{r.passed}", "iterations": it}
                    if sec_retry:
                        raise BuildFailed(f"{name}: security reject: {'; '.join(reasons)[:300]}")
                    sec_retry, max_it = True, CAP_ITER + CAP_ITER_BONUS
                    feedback = "The security review rejected tool.py:\n- " + "\n- ".join(reasons) + "\nFix it."
                else:
                    total = r.passed + r.failed + r.errors
                    self.trace("P3", name, f"tests {r.passed}/{total} pass at iteration {it}"
                                           + (" (timeout)" if r.timed_out else ""))
                    feedback, pro = f"Tests: {r.passed}/{total} pass.\n{_excerpt(r.log)}\nFix tool.py.", True
            if it >= max_it:
                raise BuildFailed(f"{name}: tests or review still fail after {it} iterations")
            msgs.append(_user(feedback))

    def code_tool_call(self, m: dict, files: dict, fn: str, args: dict) -> str:
        name = m["name"]
        if fn == "probe":
            self.check_time()
            extra = {"tool.py": files["tool.py"]} if files["tool.py"] else None
            r = runner.probe(str(args.get("code", "")), extra, timeout_s=30)
            self.trace("P3", name, f"probe: exit {r.exit}" + (" (timeout)" if r.timed_out else ""))
            return (f"exit={r.exit}{' TIMEOUT' if r.timed_out else ''}\nstdout:\n{r.stdout[-2500:]}\n"
                    f"stderr:\n{r.stderr[-1500:]}")
        if fn == "request_package":
            pkg = str(args.get("name", "")).strip()
            if not pkgindex.in_catalog(pkg):
                return f"{pkg}: not in the catalog. Use the stdlib or a catalog package."
            with self.pkg_lock:
                ok, log = (True, "already installed") if pkgindex.is_installed(pkg) else pkgindex.install(pkg)
            if ok and pkg not in m["deps"]:
                m["deps"].append(pkg)
            self.trace("P3", name, f"request_package {pkg}: {'ok' if ok else 'failed'}")
            return f"{pkg}: {'installed' if ok else 'install failed'}\n{log[-600:]}"
        return f"unknown tool {fn}"

    def dispute(self, m: dict, files: dict, tmsgs: list, claim: str) -> str:
        name, old = m["name"], self.old.get(m["name"])
        self.trace("P3", name, f"dispute: {claim[:100]}")
        tmsgs.append(_user(f"The coder disputes a test:\nDISPUTE: {claim[:1500]}\nIf the test is wrong, reply with the "
                           + ("corrected additional tests (the current tests stay)." if old else
                              "full corrected test_tool.py.") + " If it is right, reply KEEP: <reason>."))
        reply = _text(self.chat("tests", tmsgs, tool=name))
        new = _block(reply, ("python", "py", ""), "def test")
        if new:
            files["test_tool.py"] = new = self.joined(name, new)
            self.trace("P2", name, "tests: rewritten after the dispute")
            return f"The test writer changed the tests. New test_tool.py:\n```python\n{new}\n```\nNow send tool.py."
        self.trace("P2", name, "tests: kept after the dispute")
        return f"The test writer keeps the tests: {reply.strip()[:600]}\nFix tool.py. No more disputes."

    # live smoke run of the entry (between P3 and P4)
    def smoke(self, m: dict, files: dict, ex: dict | None = None) -> tuple[list[str], str]:
        """One live root run of the draft entry with its first example args, through the real sub-tools
        (new small tools as drafts; it waits for them). Mocked tests cannot see the real data shape; this run can."""
        if not isinstance(ex, dict):
            ex = next((e["args"] for e in m.get("examples") or []
                       if isinstance(e, dict) and isinstance(e.get("args"), dict)), None)
        if self.ctx.chain is None or ex is None:
            return [], ""
        wait(self.small_futs)
        if any(f.exception() for f in self.small_futs):
            raise BuildFailed(f"{m['name']}: a sub-tool failed, so no smoke run")
        with self.lock:
            overlay = {n: {"name": n, "version": 0, "manifest": b["manifest"], "files": b["files"],
                           "uses": b["manifest"].get("uses", []), "permissions": b["manifest"].get("permissions", {})}
                       for n, b in self.built.items()}
        tool = {"name": m["name"], "version": m.get("version", 0), "manifest": m, "files": dict(files),
                "uses": m.get("uses", []), "permissions": m.get("permissions", {})}
        self.check_time()
        r = self.ctx.chain.smoke(tool, ex, self.ctx.grant, overlay)
        problems, sample = smoke_problems(r)
        self.trace("P3", m["name"], f"smoke run {_js(ex)[:70]}: " + (problems[0][:100] if problems else
                   f"ok, {len(r.get('subcalls') or [])} subcalls, {r.get('duration_ms', 0) / 1000:.1f}s"))
        if not problems:
            return [], ""
        return problems, (f"Smoke run on REAL data (example args {_js(ex)[:300]}, real sub-tools):\n- "
                          + "\n- ".join(problems) + f"\nOutput sample:\n{sample}\n"
                          "The mocked tests pass, but real data does not work. Probe the real data shape, fix tool.py, "
                          "and keep the tests passing (fix a test fixture only if real data proves it wrong).")

    # P4 security
    def review(self, m: dict, files: dict, content_hash: str, r) -> tuple[str, list[str]]:
        name = m["name"]
        issues = static_check.check(files["tool.py"], m)
        if issues:
            verdict, reasons = "reject", [f"line {i['line']}: {i['rule']}: {i['msg']}" for i in issues]
        else:
            msgs = [_sys("security"), _user(
                f"manifest.json:\n{_js(m)}\n\ntool.py:\n```python\n{files['tool.py']}\n```\n\n"
                f"Static check: pass\nTests: {r.passed}/{r.passed} pass")]
            v = _json_obj(_text(self.chat("security", msgs, tool=name))) or {}
            verdict = "approve" if v.get("verdict") == "approve" else "reject"
            reasons = [str(x) for x in v.get("reasons") or []] or ([] if verdict == "approve" else ["no valid verdict"])
        self.db.record_review(content_hash, verdict, _js({"static": issues, "reasons": reasons}))
        self.trace("P4", name, f"security: {verdict}" + (f": {reasons[0][:120]}" if verdict == "reject" else
                                                         " (static ok)"))
        return verdict, reasons

    # one tool: P2 -> P3 -> P4 (runs in a pool thread)
    def install_deps(self, specs: list[dict]) -> None:
        for m in specs:
            for dep in m.get("deps") or []:
                if not pkgindex.is_installed(dep):
                    ok, _ = pkgindex.install(dep)
                    self.trace("P2", m["name"], f"install {dep}: {'ok' if ok else 'failed'}")
                    if not ok:
                        self.warn(f"{m['name']}: dep {dep} did not install")

    def make_tool(self, m: dict, smoke: bool = False) -> dict:
        name, old = m["name"], self.old.get(m["name"])
        failure = self.failure if old else ""
        ifaces = self.interfaces(m.get("uses") or [])
        tests, tmsgs = self.write_tests(m, ifaces, failure, old)
        files = {"tool.py": old["files"]["tool.py"] if old else "", "test_tool.py": tests,
                 "SKILL.md": old["files"].get("SKILL.md", "") if old else ""}
        built = self.code_tool(m, files, ifaces, tmsgs, failure, old, smoke)
        with self.lock:
            self.built[name] = built
        return built

    # P5 handoff
    def handoff(self, entry: str, entry_row: dict | None = None) -> dict:
        def reused(name: str, row: dict) -> dict:
            m = row["manifest"]
            return {"name": name, "grade": m.get("grade"), "status": "reused", "version": row.get("version"),
                    "uses": m.get("uses", []), "deps": m.get("deps", []), "permissions": m.get("permissions"),
                    "tests": "-", "iterations": 0, "verdict": "-", "cost_usd": 0.0, "summary": m.get("summary")}

        names, rows = list(self.specs), {}   # plan order: small tools, then the entry (when it is built)
        for i, name in enumerate(names):
            b = self.built[name]
            m = b["manifest"]
            cost = self.tool_cost.get(name, 0.0) + (self.tool_cost.get("_plan", 0.0) if i == len(names) - 1 else 0.0)
            self.db.save_draft(self.ctx.build_id, m, b["files"], round(cost, 6))  # final cost -> build_cost_usd
            rows[name] = {"name": name, "grade": m["grade"], "status": "new", "content_hash": b["content_hash"],
                          "perm_hash": b["perm_hash"], "uses": m["uses"], "deps": m["deps"],
                          "permissions": m["permissions"], "tests": b["tests"], "iterations": b["iterations"],
                          "verdict": "approve", "cost_usd": round(cost, 4), "summary": m["summary"]}
            if m.get("parent"):
                rows[name]["parent"] = m["parent"]
        tree = [rows[n] for n in names if n != entry]
        tree += [reused(n, r) for n, r in self.reuse.items() if n != entry and n not in rows]
        tree.append(reused(entry, entry_row) if entry_row else rows[entry])
        return {"type": "handoff", "build_id": self.ctx.build_id, "entry": entry, "tree": tree,
                "cost_usd": round(self.cost, 4), "skill": (entry_row or self.built.get(entry))["files"]["SKILL.md"],
                "warnings": self.warnings}

    def run(self) -> dict:
        plan = self.plan()
        self.reuse.update(plan["reuse"])
        self.old, self.samples = plan["old"], plan["samples"]
        if self.ctx.repair_of and plan["notes"]:
            self.failure += f"\nCause (the planner's diagnosis): {plan['notes']}"
        entry, entry_row = plan["entry"], plan["entry_row"]
        jobs = plan["new"] + ([] if entry_row else [entry])
        self.specs = {m["name"]: m for m in jobs}
        self.trace("P1", entry["name"], f"plan: entry {entry['name']} ({entry['grade']}{', reused' if entry_row else ''})"
                   f"; new {[m['name'] for m in jobs if not m.get('parent')] or '-'}"
                   f"; extend {[m['parent'] for m in jobs if m.get('parent')] or '-'}; reuse {list(self.reuse) or '-'}")
        self.install_deps(jobs)
        with ThreadPoolExecutor(WORKERS) as ex:
            self.small_futs = [ex.submit(self.make_tool, m) for m in plan["new"]]
            futs = self.small_futs + ([] if entry_row else [ex.submit(self.make_tool, entry, True)])
            wait(futs, return_when=FIRST_EXCEPTION)
            err = next((f.exception() for f in futs if f.done() and f.exception()), None)
            if err:
                self.stop.set()   # the other jobs stop at their next LLM call
        if err:
            raise err
        if entry_row:             # a sub-tool was the target: one live run of the old entry over the new sub-tools
            problems, _ = self.smoke(entry_row["manifest"], entry_row["files"], self.fail_args)
            if problems:
                self.warn(f"{entry['name']}: smoke run over the new sub-tools: {'; '.join(problems)[:300]}")
        h = self.handoff(entry["name"], entry_row)
        new = sum(t["status"] == "new" for t in h["tree"])
        self.trace("P5", entry["name"], f"handoff: {new} new, {len(h['tree']) - new} reused, ${self.cost:.4f}")
        return h


def build(ctx: BuildCtx, emit: Callable[[dict], None]) -> dict:
    """Runs one build and returns the handoff dict, or the failed event. Both are also emitted."""
    db = ctx.db
    db.start_build(ctx.build_id, ctx.session_id, ctx.task, ctx.need, ctx.lookup_id, ctx.repair_of)
    chef = Chef(ctx, emit)
    try:
        h = chef.run()
    except llm.CapExceeded as e:
        reason = f"cap: meter refused ({_js(getattr(e, 'info', {}))})"
    except BuildFailed as e:
        reason = str(e)
    except Exception as e:  # noqa: BLE001 - a build must always end with a failed event
        reason = f"error: {type(e).__name__}: {str(e)[:300]}"
    else:
        chef.emit(h)
        db.finish_build(ctx.build_id, "handoff", round(chef.cost, 6), h)
        return h
    ev = {"type": "failed", "reason": reason, "cost_usd": round(chef.cost, 4)}
    chef.emit(ev)
    db.finish_build(ctx.build_id, "failed", round(chef.cost, 6), None)
    return ev
