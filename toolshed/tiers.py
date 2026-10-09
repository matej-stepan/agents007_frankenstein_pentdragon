"""Package tiers for the toolshed image (reads packages.json next to this file or /opt/shed/packages.json).

python3 tiers.py A        -> tier A names, one per line (baked into the image)
python3 tiers.py catalog  -> every catalog name, one per line (request_package may install these)
python3 tiers.py system   -> the apt packages the image installs
"""

import json
import sys
from pathlib import Path

# Tier A: useful for most scraping/data tools and fast to install (wheels only, no heavy, no source_build).
TIER_A = [
    # essentials
    "requests", "httpx", "urllib3", "certifi", "pydantic", "python-dotenv", "pyyaml", "orjson",
    "python-dateutil", "tzdata", "pytz", "tenacity", "attrs", "typing-extensions", "packaging", "regex",
    "more-itertools", "cachetools", "jsonschema", "jmespath", "python-slugify", "charset-normalizer", "idna",
    "jinja2", "markupsafe", "backoff", "lxml", "pillow", "numpy", "tqdm", "humanize", "anyio",
    # testing (the Chef always runs pytest before install)
    "pytest", "pytest-mock", "pytest-timeout", "respx", "responses", "freezegun", "pytest-httpx",
    # web and scraping
    "beautifulsoup4", "html5lib", "selectolax", "parsel", "cssselect", "feedparser", "tldextract",
    "lxml-html-clean", "w3lib", "fake-useragent",
    # data, text, documents
    "pandas", "tabulate", "rapidfuzz", "unidecode", "dateparser", "price-parser", "babel",
    "pypdf", "openpyxl", "markdown", "html2text", "markdownify", "xmltodict", "defusedxml",
]

# apt packages the image installs (a small subset of packages.json "system").
SYSTEM = ["ca-certificates", "curl", "git", "jq", "sqlite3", "unzip", "xz-utils"]


def catalog_path() -> Path:
    for p in (Path(__file__).with_name("packages.json"), Path("/opt/shed/packages.json")):
        if p.exists():
            return p
    raise SystemExit("packages.json not found")


def load() -> dict:
    return json.loads(catalog_path().read_text())


def catalog(data: dict | None = None) -> dict[str, dict]:
    """name -> {category, heavy, source_build, note}; first category wins."""
    data = data or load()
    out: dict[str, dict] = {}
    for cat, body in data["categories"].items():
        for p in body["packages"]:
            out.setdefault(p["name"], {"category": cat, **{k: v for k, v in p.items() if k != "name"}})
    return out


def tier_a(data: dict | None = None) -> list[str]:
    cat = catalog(data)
    bad = [n for n in TIER_A if n not in cat or cat[n].get("heavy") or cat[n].get("source_build")]
    if bad:
        raise SystemExit(f"tier A names not in catalog or not fast: {bad}")
    return list(dict.fromkeys(TIER_A))


def main(argv: list[str]) -> None:
    what = argv[1] if len(argv) > 1 else "A"
    names = {"A": tier_a, "catalog": lambda: list(catalog()), "system": lambda: SYSTEM}.get(what)
    if not names:
        raise SystemExit("usage: tiers.py A|catalog|system")
    print("\n".join(names()))


if __name__ == "__main__":
    main(sys.argv)
