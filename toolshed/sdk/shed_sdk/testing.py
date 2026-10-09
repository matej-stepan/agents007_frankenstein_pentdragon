"""Test helpers for tool tests: MockShed (no socket, no LLM, no network) and the live marker."""

import copy
import json as _json
import sys
import tempfile
from pathlib import Path

from shed_sdk import Shed, ShedError

try:
    import pytest

    live = pytest.mark.live  # at most one live smoke test per tool (it may use the network)
except ImportError:  # pragma: no cover
    def live(f):
        return f


class MockShed(Shed):
    def __init__(self, work: str | Path | None = None, registry: list[dict] | None = None):
        super().__init__(sock="/nonexistent", token="")
        # Tests never write into the real /work: default to a throwaway dir.
        self.work = Path(work) if work is not None else Path(tempfile.mkdtemp(prefix="mockshed-"))
        self.out_dir = self.work / "out"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._mocks: dict = {}
        self._llm = None
        self._registry = registry or []
        self.calls: list[tuple[str, dict]] = []
        self.llm_prompts: list = []

    def mock(self, name: str, value_or_fn) -> None:
        self._mocks[name] = value_or_fn

    def mock_llm(self, value_or_fn) -> None:
        self._llm = value_or_fn

    def call(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name not in self._mocks:
            raise ShedError(f"MockShed: no mock for tool {name!r}")
        v = self._mocks[name]
        if isinstance(v, Exception):
            raise v
        return v(args) if callable(v) else copy.deepcopy(v)

    def llm(self, prompt, *, max_tokens: int = 1024, json: bool = False) -> str:
        self.llm_prompts.append(prompt)
        if self._llm is None:
            raise ShedError("MockShed: no mock_llm set")
        v = self._llm(prompt) if callable(self._llm) else self._llm
        return v if isinstance(v, str) else _json.dumps(v)

    def registry(self) -> list[dict]:
        return copy.deepcopy(self._registry)

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr)
