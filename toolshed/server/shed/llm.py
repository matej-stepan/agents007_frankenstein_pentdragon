"""LLM calls from the toolshed go to the host meter over /run/meter/meter.sock (the key never enters B)."""

import os

try:
    from shed import llmclient  # the image: cli/taltempla/llm.py copied here
except ImportError:  # host dev tree
    from taltempla import llm as llmclient

METER_SOCK = os.environ.get("SHED_METER_SOCK", "/run/meter/meter.sock")
DEFAULT_MODEL = "deepseek-flash"


class CapExceeded(Exception):
    """The meter refused the call (HTTP 402): a USD cap is used up."""

    def __init__(self, info: dict):
        super().__init__(f"cap exceeded: {info}")
        self.info = info


class GrantRejected(Exception):
    """The meter did not accept the grant (HTTP 401)."""


def available() -> bool:
    return os.path.exists(METER_SOCK)


def chat(grant: str, body: dict, role: str, tool: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {grant}", "X-Taltempla-Role": role}
    if tool:
        headers["X-Taltempla-Tool"] = tool
    body = {"model": DEFAULT_MODEL, **body}
    try:
        return llmclient.post_json(body, unix_socket=METER_SOCK, path="/v1/chat/completions", headers=headers)
    except llmclient.LLMError as e:
        err = e.body.get("error", {}) if isinstance(e.body, dict) else {}
        if e.status == 402:
            raise CapExceeded({k: v for k, v in err.items() if k != "type"} if isinstance(err, dict) else {}) from None
        if e.status == 401:
            raise GrantRejected("the meter rejected the grant") from None
        raise


text = llmclient.text
