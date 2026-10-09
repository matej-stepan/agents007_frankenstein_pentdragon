"""Lookup: bm25 + coverage, fit thresholds, big tools first, <= 3 rows (temp DB)."""

from shed import lookup as lk
from shed.db import DB


def add(db, name, grade, summary, keywords, uses=(), description=None):
    m = {"name": name, "grade": grade, "summary": summary, "description": description or summary, "keywords": keywords,
         "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "uses": list(uses), "deps": [],
         "permissions": {"network": True, "llm_usd": 0, "files": "none"}, "limits": {}, "examples": [{"args": {}}]}
    files = {"tool.py": "def run(args, shed):\n    return {}\n", "test_tool.py": "def test_x():\n    pass\n",
             "SKILL.md": summary}
    h = db.save_draft("b", m, files)["content_hash"]
    db.record_test_run(h, "tests", 1, 0, "")
    db.record_review(h, "approve", "")
    db.register(h, {"mode": "once", "by": "operator"})


def test_empty_registry_is_fit_none(tmp_path):
    db = DB(tmp_path / "s.db")
    r = lk.lookup(db, "cheapest house in Brno under 8M CZK", "s1")
    assert r["fit"] == "none" and r["rows"] == [] and db.get_lookup(r["lookup_id"])["session_id"] == "s1"


def test_fit_and_order(tmp_path):
    db = DB(tmp_path / "s.db")
    add(db, "fetch_page", "small", "Fetch a web page and return its HTML", ["http", "html", "download"])
    add(db, "parse_listings", "small", "Parse real estate listings from HTML", ["listings", "real estate", "parse"])
    add(db, "csv_export", "small", "Write rows to a CSV file", ["csv", "export"])
    add(db, "pdf_text", "small", "Extract text from PDF files", ["pdf", "text"])
    add(db, "house_finder", "big", "Find houses for sale in a Czech region with price filters",
        ["house", "real estate", "price", "region", "czech"], uses=["fetch_page", "parse_listings"])
    r = lk.lookup(db, "find houses for sale in Czech region under a price", "s1")
    assert r["fit"] == "good" and r["rows"][0]["name"] == "house_finder" and len(r["rows"]) <= 3
    r = lk.lookup(db, "parse car listings html", "s1")
    assert r["fit"] in ("good", "partial") and r["rows"][0]["name"] in ("parse_listings", "fetch_page")
    assert lk.lookup(db, "translate a poem to latin", "s1")["fit"] == "none"
    assert len(lk.explore(db, "", page=0, k=2)) == 2 and lk.explore(db, "")[0]["grade"] == "big"
    assert lk.tool_detail(db, "house_finder")["uses"] == ["fetch_page", "parse_listings"]


def test_prefix_only_on_name_summary_keywords(tmp_path):
    db = DB(tmp_path / "s.db")
    add(db, "deck_shuffle", "small", "Shuffle a deck", ["deck"], description="Works with playing cards")
    add(db, "carousel_feed", "small", "Read a carousel feed", ["feed"])
    names = [r["name"] for r in lk.lookup(db, "car*", "s1")["rows"]]
    assert names == ["carousel_feed"]  # "cards" only in a description: no prefix hit
    r = lk.lookup(db, "card deck price", "s1")["rows"][0]  # the whole term still matches a description
    assert r["name"] == "deck_shuffle" and r["uncovered"] == ["price"] and r["score"] == 0.67
