"""Lookup without an LLM: FTS5 bm25 over the active tools plus a coverage score. Contract sections 5 and 10."""

import re
import unicodedata

GOOD = 0.6      # coverage >= GOOD -> fit good
PARTIAL = 0.25  # coverage >= PARTIAL -> fit partial
MAX_ROWS = 3
WEIGHTS = (5.0, 3.0, 4.0, 1.0, 1.0)  # name, summary, keywords, description, skill
FIT_RANK = {"good": 2, "partial": 1, "none": 0}
PREFIX_COLS = "{name summary keywords}"  # prefix match only here: car* must not hit "cards" in a description

STOPWORDS = set("""
a an the and or but if then else of in on at to for from by with without into onto over under about as is are was
were be been being do does did done have has had it its this that these those there here i me my we our you your he
she they them their what which who whom whose when where why how all any each some most more less few many much
can could should would will shall may might must not no yes so than too very just also only please find get give show
tell list make want need help using use via per up down out off again once me let lets something anything thing things
""".split())  # noqa: SIM905


def terms(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", text)
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS and len(w) > 1))


def _term(t: str) -> str:
    """FTS5 expression for one query term: a prefix on name, summary, keywords; the whole term anywhere."""
    return f'({PREFIX_COLS} : "{t}"*) OR "{t}"'


def _match(db, expr: str) -> dict[str, float]:
    """name -> bm25 (lower is better) for an FTS5 expression."""
    rows = db.q(f"SELECT name, bm25(tools_fts, {', '.join(map(str, WEIGHTS))}) AS r FROM tools_fts "
                "WHERE tools_fts MATCH ?", (expr,))
    out: dict[str, float] = {}
    for r in rows:
        name = r["name"].split()[-1]
        out[name] = min(out.get(name, 0.0), r["r"])
    return out


def score(db, query: str) -> list[dict]:
    """All matching active tools, best first: {name, grade, version, summary, uses, score, fit, uncovered, bm25}."""
    ts = terms(query)
    if not ts:
        return []
    per_term = {t: set(_match(db, _term(t))) for t in ts}
    ranks = _match(db, " OR ".join(f"({_term(t)})" for t in ts))
    tools = {t["name"]: t for t in db.active_tools()}
    rows = []
    for name, bm in ranks.items():
        t = tools.get(name)
        if not t:
            continue
        uncovered = [t for t in ts if name not in per_term[t]]
        cov = 1 - len(uncovered) / len(ts)
        fit = "good" if cov >= GOOD else "partial" if cov >= PARTIAL else "none"
        rows.append({"name": name, "grade": t["grade"], "version": t["version"], "summary": t["summary"],
                     "uses": t["uses"], "score": round(cov, 2), "fit": fit, "uncovered": uncovered, "bm25": bm})
    rows.sort(key=lambda r: (-FIT_RANK[r["fit"]], r["grade"] != "big", -r["score"], r["bm25"]))
    return rows


def lookup(db, query: str, session_id: str) -> dict:
    rows = [{k: v for k, v in r.items() if k != "bm25"} for r in score(db, query)[:MAX_ROWS]]
    fit = max((r["fit"] for r in rows), key=FIT_RANK.get, default="none")
    lookup_id = db.save_lookup(session_id, query, fit, rows)
    return {"lookup_id": lookup_id, "fit": fit, "rows": rows}


def explore(db, query: str, page: int = 0, k: int = 5) -> list[dict]:
    if query and query.strip():
        rows = [{k_: v for k_, v in r.items() if k_ not in ("bm25", "fit")} for r in score(db, query)]
    else:
        rows = [{"name": t["name"], "grade": t["grade"], "version": t["version"], "summary": t["summary"],
                 "uses": t["uses"], "score": 0.0} for t in db.active_tools()]
    return rows[page * k:(page + 1) * k]


def tool_detail(db, name: str) -> dict | None:
    t = db.get_version(name)
    if not t:
        return None
    return {k: t[k] for k in ("name", "version", "grade", "skill", "input_schema", "output_schema", "uses",
                              "permissions")}
