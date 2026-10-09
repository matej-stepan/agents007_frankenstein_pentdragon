"""LLM calls from the toolshed go to the host meter over /run/meter/meter.sock (the key never enters B)."""

import os

try:
    from shed import llmclient  # the image: cli/taltempla/llm.py copied here
except ImportError:  # host dev tree
    from taltempla import llm as llmclient

METER_SOCK = os.environ.get("SHED_METER_SOCK", "/run/meter/meter.sock")
DEFAULT_MODEL = "deepseek-flash"
CHAT_TIMEOUT = 1500  # s: the meter may hold a call up to 600 s for in-flight reserves, then the call (600 s) runs


class CapExceeded(Exception):
    """The meter refused the call (HTTP 402): a USD cap is used up."""

    def __init__(self, info: dict):
        super().__init__(f"cap exceeded: {info}")
        self.info = info


class GrantRejected(Exception):
    """The meter did not accept the grant (HTTP 401)."""


def available() -> bool:
    return os.path.exists(METER_SOCK)


def chat(grant: str, body: dict, role: str, tool: str | None = None, wait_s: float | None = None) -> dict:
    """wait_s bounds the meter's wait for other calls' reserves (None: the meter's default; 0 for role tool)."""
    headers = {"Authorization": f"Bearer {grant}", "X-Taltempla-Role": role}
    if wait_s is not None:
        headers["X-Taltempla-Wait"] = f"{max(0.0, wait_s):.0f}"
    if tool:
        headers["X-Taltempla-Tool"] = tool
    body = {"model": DEFAULT_MODEL, **body}
    try:
        return llmclient.post_json(body, unix_socket=METER_SOCK, path="/v1/chat/completions", headers=headers,
                                  timeout=CHAT_TIMEOUT)
    except llmclient.LLMError as e:
        err = e.body.get("error", {}) if isinstance(e.body, dict) else {}
        if e.status == 402:
            raise CapExceeded({k: v for k, v in err.items() if k != "type"} if isinstance(err, dict) else {}) from None
        if e.status == 401:
            raise GrantRejected("the meter rejected the grant") from None
        raise


def topup_poll(grant: str) -> dict:
    """The operator's last answer (POST /v1/topup): {"decision": "pending" | "denied" | "raised", "seq", ...}.
    A revoked grant (401) or a meter without top-ups (404) is "denied" (nothing can change); other errors "pending"."""
    try:
        r = llmclient.post_json({}, unix_socket=METER_SOCK, path="/v1/topup",
                                headers={"Authorization": f"Bearer {grant}"}, timeout=10)
    except llmclient.LLMError as e:
        return {"decision": "denied" if e.status in (401, 404) else "pending"}
    except Exception:  # noqa: BLE001 - a poll never fails the build
        return {"decision": "pending"}
    return r if isinstance(r, dict) and r.get("decision") in ("pending", "denied", "raised") else {"decision": "pending"}


text = llmclient.text
