"""Lookup: bm25 + coverage, fit thresholds, big tools only for the agent (D53), <= 3 rows (temp DB)."""

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
    r = lk.lookup(db, "parse car listings html", "s1")  # only small tools match: no rows, fit partial (big_chef)
    assert r["fit"] == "partial" and r["rows"] == [] and "big_chef" in r["note"]
    assert db.get_lookup(r["lookup_id"])["rows"][0]["name"] in ("parse_listings", "fetch_page")  # for the Chef plan
    assert lk.lookup(db, "translate a poem to latin", "s1")["fit"] == "none"
    assert len(lk.explore(db, "", page=0, k=2)) == 2 and lk.explore(db, "")[0]["grade"] == "big"
    assert lk.tool_detail(db, "house_finder")["uses"] == ["fetch_page", "parse_listings"]
    assert lk.agent_tool_detail(db, "house_finder")["input_schema"] == {"type": "object"}
    small = lk.agent_tool_detail(db, "fetch_page")
    assert "input_schema" not in small and "small building block" in small["note"]


def test_small_tool_is_a_part_not_a_row(tmp_path):
    db = DB(tmp_path / "s.db")
    add(db, "download_file", "small", "Download a file from a URL", ["download", "file", "url", "image"])
    r = lk.lookup(db, "download image file", "s1")       # the small tool covers every term
    assert r["fit"] == "partial" and r["rows"] == []
    assert db.get_lookup(r["lookup_id"])["fit"] == "partial"  # the gap rule lets big_chef build on it
    assert lk.explore(db, "download image file")[0]["name"] == "download_file"  # the Chef still sees it
    add(db, "image_fetcher", "big", "Find and download images of a subject", ["image", "download", "search", "file"],
        uses=["download_file"])
    r = lk.lookup(db, "download image file", "s1")
    assert r["fit"] == "good" and [x["name"] for x in r["rows"]] == ["image_fetcher"] and "note" not in r


def test_prefix_only_on_name_summary_keywords(tmp_path):
    db = DB(tmp_path / "s.db")
    add(db, "deck_shuffle", "small", "Shuffle a deck", ["deck"], description="Works with playing cards")
    add(db, "carousel_feed", "small", "Read a carousel feed", ["feed"])
    names = [r["name"] for r in lk.score(db, "car*")]
    assert names == ["carousel_feed"]  # "cards" only in a description: no prefix hit
    r = lk.score(db, "card deck price")[0]  # the whole term still matches a description
    assert r["name"] == "deck_shuffle" and r["uncovered"] == ["price"] and r["score"] == 0.67
