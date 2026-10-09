"""Tool manifest: content hash, permission hash and a light validation (contract/manifest.schema.json rules)."""

import hashlib
import json
import re

FILES = ("tool.py", "test_tool.py", "SKILL.md")
REQUIRED = ("name", "grade", "summary", "description", "keywords", "input_schema", "output_schema",
            "uses", "deps", "permissions", "limits", "examples")
ALLOWED = set(REQUIRED) | {"version", "parent"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
SKILL_MAX = 1200


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def content_hash(manifest: dict, files: dict[str, str]) -> str:
    m = {k: v for k, v in manifest.items() if k not in ("version", "parent")}
    return sha({"manifest": m, "files": {k: files.get(k, "") for k in FILES}})


def perm_hash(manifest: dict) -> str:
    return sha({k: manifest.get(k) for k in ("name", "uses", "deps", "permissions")})


def validate(manifest: dict, files: dict[str, str] | None = None) -> list[str]:
    """Return a list of problems; empty = valid."""
    m, errs = manifest, []
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    errs += [f"missing field: {k}" for k in REQUIRED if k not in m]
    errs += [f"unknown field: {k}" for k in m if k not in ALLOWED]
    if not isinstance(m.get("name"), str) or not NAME_RE.match(m.get("name", "")):
        errs.append("name must match ^[a-z][a-z0-9_]{2,40}$")
    if m.get("grade") not in ("big", "small"):
        errs.append("grade must be big or small")
    if not isinstance(m.get("summary", ""), str) or len(m.get("summary", "")) > 120:
        errs.append("summary must be a string of at most 120 chars")
    if not isinstance(m.get("description", ""), str):
        errs.append("description must be a string")
    for k, lim in (("keywords", 16), ("uses", None), ("deps", None)):
        v = m.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append(f"{k} must be a list of strings")
        elif lim and len(v) > lim:
            errs.append(f"{k} has more than {lim} items")
    for k in ("input_schema", "output_schema"):
        if not isinstance(m.get(k, {}), dict):
            errs.append(f"{k} must be an object")
    # A big tool is task level; it may do all its work itself (uses can be empty).
    if m.get("name") in (m.get("uses") or []):
        errs.append("a tool cannot use itself")
    p = m.get("permissions")
    if not isinstance(p, dict):
        errs.append("permissions must be an object")
    else:
        if set(p) != {"network", "llm_usd", "files"}:
            errs.append("permissions needs exactly network, llm_usd, files")
        if not isinstance(p.get("network"), bool):
            errs.append("permissions.network must be a boolean")
        llm = p.get("llm_usd")
        if not isinstance(llm, (int, float)) or isinstance(llm, bool) or not 0 <= llm <= 0.25:
            errs.append("permissions.llm_usd must be a number in 0..0.25")
        if p.get("files") not in ("none", "read", "write"):
            errs.append("permissions.files must be none, read or write")
    lim = m.get("limits", {})
    if not isinstance(lim, dict) or set(lim) - {"timeout_s", "memory_mb"}:
        errs.append("limits allows only timeout_s and memory_mb")
    else:
        t, mem = lim.get("timeout_s", 60), lim.get("memory_mb", 2048)
        if not isinstance(t, int) or not 1 <= t <= 180:
            errs.append("limits.timeout_s must be an integer in 1..180")
        if not isinstance(mem, int) or not 128 <= mem <= 4096:
            errs.append("limits.memory_mb must be an integer in 128..4096")
    ex = m.get("examples")
    if not isinstance(ex, list) or not ex or not all(isinstance(e, dict) and isinstance(e.get("args"), dict)
                                                      for e in ex):
        errs.append("examples must be a non-empty list of {args: object}")
    if "version" in m and (not isinstance(m["version"], int) or m["version"] < 1):
        errs.append("version must be an integer >= 1")
    if files is not None:
        errs += [f"missing file: {f}" for f in FILES if not files.get(f)]
        if len(files.get("SKILL.md", "")) > SKILL_MAX:
            errs.append(f"SKILL.md is longer than {SKILL_MAX} chars")
    return errs
