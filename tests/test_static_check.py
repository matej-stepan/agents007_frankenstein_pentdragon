"""P4 static check: the obvious escapes are caught, a clean tool passes."""

from shed.chef.static_check import check

MANIFEST = {"name": "listing_search", "uses": ["fetch_page"],
            "permissions": {"network": True, "llm_usd": 0, "files": "write"}}

CLEAN = '''
import json
import urllib.request
from pathlib import Path


def http_get(url, *, params=None, headers=None, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.geturl(), r.read().decode()


def run(args, shed):
    if not args.get("query"):
        raise ValueError("query is required")
    page = shed.call("fetch_page", {"url": "https://example.com/?q=" + args["query"]})
    Path("/work/out/last.json").write_text(json.dumps(page))
    open("/tmp/scratch.txt", "w").write("x")
    return {"results": [page], "warnings": [], "sources": ["https://example.com"]}
'''


def rules(code, manifest=MANIFEST):
    return {i["rule"] for i in check(code, manifest)}


def test_clean_tool_passes():
    assert check(CLEAN, MANIFEST) == []


def test_catches_subprocess():
    assert "import" in rules("import subprocess\ndef run(args, shed):\n    return subprocess.run(['ls'])\n")
    assert "import" in rules("from subprocess import run as r\n")
    assert "call" in rules("import os\nos.system('id')\n")


def test_catches_eval_and_dunders():
    assert "call" in rules("def run(args, shed):\n    return eval(args['x'])\n")
    assert "dunder" in rules("x = ().__class__.__bases__[0].__subclasses__()\n")


def test_catches_env():
    assert "call" in rules("import os\nkey = os.environ['OAI_COMPATIBLE_KEY']\n")
    assert "import" in rules("from os import getenv\n")
    assert "call" in rules("import os as o\nt = o.getenv('SHED_RUN_TOKEN')\n")


def test_catches_paths():
    assert "path" in rules("open('/etc/passwd').read()\n")
    assert "path" in rules("from pathlib import Path\nPath('/data/shed.db').read_bytes()\n")


def test_catches_dynamic_and_foreign_shed_call():
    assert "shed_call" in rules("def run(args, shed):\n    return shed.call(args['tool'], {})\n")
    assert "shed_call" in rules("def run(args, s):\n    return s.call('delete_everything', {})\n")
    assert "shed_call" in rules("def run(args, shed):\n    c = shed.call\n    return c('fetch_page', {})\n")


def test_permissions_must_match():
    no_net = {**MANIFEST, "permissions": {"network": False, "llm_usd": 0, "files": "none"}}
    assert "network" in rules("import requests\n", no_net)
    assert "llm" in rules("def run(args, shed):\n    return {'a': shed.llm('hi')}\n", no_net)
