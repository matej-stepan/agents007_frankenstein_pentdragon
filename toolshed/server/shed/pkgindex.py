"""Package catalog search and on-demand installs (uv pip install --system, catalog names only). Section 10."""

import importlib
import importlib.metadata
import json
import re
import subprocess
import threading
from functools import cache
from pathlib import Path

CATALOG_PATHS = (Path("/opt/shed/packages.json"), Path(__file__).resolve().parents[2] / "packages.json")
_db = None
_lock = threading.Lock()


def init(db) -> None:
    """Record installs in this DB (shedd start)."""
    global _db
    _db = db


@cache
def catalog() -> dict[str, dict]:
    """name -> {category, description, note, heavy, source_build}."""
    for p in CATALOG_PATHS:
        if p.exists():
            data = json.loads(p.read_text())
            break
    else:
        return {}
    out: dict[str, dict] = {}
    for cat, body in data.get("categories", {}).items():
        for pkg in body.get("packages", []):
            e = out.setdefault(pkg["name"], {"category": cat, "categories": [], "description": body.get("description", ""),
                                             "note": pkg.get("note", ""), "heavy": bool(pkg.get("heavy")),
                                             "source_build": bool(pkg.get("source_build"))})
            e["categories"].append(cat)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.split("[")[0].strip().lower())


def _resolve(name: str) -> str | None:
    cat = catalog()
    if name in cat:
        return name
    n = _norm(name)
    return next((c for c in cat if _norm(c) == n), None)


def in_catalog(name: str) -> bool:
    return _resolve(name) is not None


def is_installed(name: str) -> bool:
    importlib.invalidate_caches()
    try:
        importlib.metadata.distribution(name.split("[")[0])
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def search(query: str, k: int = 8) -> list[dict]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    rows = []
    for name, e in catalog().items():
        hay_name = name.lower()
        hay = " ".join([*e["categories"], e["description"], e["note"]]).lower()
        s = sum(3 if w == hay_name else 2 if w in hay_name else 1 if w in hay else 0 for w in words)
        if s:
            rows.append((s, not e["heavy"], name, e))
    rows.sort(key=lambda r: (-r[0], not r[1], r[2]))
    return [{"name": n, "category": e["category"], "installed": is_installed(n)} for _, _, n, e in rows[:k]]


def install(name: str, timeout_s: int = 300) -> tuple[bool, str]:
    real = _resolve(name)
    if not real:
        return False, f"{name} is not in the package catalog"
    if is_installed(real):
        return True, f"{real} is already installed"
    with _lock:
        try:
            p = subprocess.run(["uv", "pip", "install", "--system", real], capture_output=True, text=True,
                               timeout=timeout_s, stdin=subprocess.DEVNULL, check=False)
            ok, log = p.returncode == 0, (p.stdout + p.stderr)[-3000:]
        except subprocess.TimeoutExpired:
            ok, log = False, f"uv pip install {real} timed out after {timeout_s}s"
        except FileNotFoundError:
            ok, log = False, "uv not found"
    if _db is not None:
        _db.record_package(real, ok, log)
        _db.record_event("package", None, None, {"name": real, "ok": ok})
    return ok, log


def reinstall_recorded(db) -> threading.Thread:
    """At shedd start: reinstall the packages that earlier builds installed (the image does not keep them)."""

    def work():
        for row in db.packages():
            if row["ok"] and not is_installed(row["name"]):
                install(row["name"])

    t = threading.Thread(target=work, name="pkg-reinstall", daemon=True)
    t.start()
    return t
