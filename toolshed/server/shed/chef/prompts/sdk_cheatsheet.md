## SDK (tool.py)
- shed.call(name, args) -> dict: runs a tool from manifest uses. Raises on failure. Depth <= 3, <= 20 subcalls per run.
  Subcalls are a budget: never shed.call once per item in a loop. Call a small tool once per page or batch, or only for the top <= 10 items; do the per-item work locally.
- shed.llm(prompt_or_messages, max_tokens=1024, json=False) -> str: only if permissions.llm_usd > 0. Metered.
- shed.registry() -> list[dict]: read-only tool stats {name, version, invocations, failures, avg_ms, build_cost_usd, llm_cost_usd}.
- shed.work = Path("/work"); shed.out_dir = Path("/work/out"); shed.log(msg) writes to stderr.
- All HTTP goes through module-level functions in tool.py, so tests can monkeypatch them:
  `http_get(url, *, params=None, headers=None, timeout=20) -> (status: int, final_url: str, text: str)`
  `http_post(url, *, data=None, json=None, headers=None, timeout=20) -> (status, final_url, text)` (only if needed).
  Send a browser-like User-Agent. Retry 429/5xx and network errors with backoff.
- Keep a result under 6 KB when you can: shedd moves larger results to /work/out and returns a preview. Write files (CSV, images) to shed.out_dir and return the path.

## Tests (test_tool.py)
```python
import pytest
import tool
from tool import run
from shed_sdk import ShedError
from shed_sdk.testing import MockShed, live

SAMPLE = '...'  # the real sample from the request (same fields, nesting and formats)

def test_parses_listing(monkeypatch):
    monkeypatch.setattr(tool, "http_get", lambda url, **kw: (200, url, SAMPLE))
    shed = MockShed()
    shed.mock("<name in uses>", lambda a: {...})  # every name in uses; a fixed value also works
    shed.mock_llm('{"label": "x"}')  # only if llm_usd > 0
    # MockShed(registry=[{...}]) feeds shed.registry(); shed.mock(name, ShedError("down")) makes that call raise
    out = run({"query": "x"}, shed)
    assert out["results"] and shed.calls[0][0] == "<name in uses>"

def test_bad_args():
    with pytest.raises(ValueError):
        run({}, MockShed())

@live  # at most one; real network; pytest.skip on network errors; assert the shape only
def test_live_smoke(): ...
```
