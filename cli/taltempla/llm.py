"""Shared OpenAI-compatible LLM client. STDLIB ONLY.

The canonical file is cli/taltempla/llm.py. The Containerfile copies it into the image as
/opt/shed/server/shed/llmclient.py, so it must not import anything outside the stdlib.

It talks to an https/http base URL (the meter -> DeepSeek) or to a unix socket (shedd -> the meter).
DeepSeek in thinking mode needs every earlier `reasoning_content` sent back with tool calls,
so assistant_message() keeps it.
"""

import http.client
import json
import socket
import ssl
import time
import urllib.parse

RETRY_STATUS = {429, 500, 503}
RETRIES = 2
BACKOFF = (1.0, 3.0)  # seconds before retry 1 and 2 (a Retry-After header wins, up to 30 s)


class LLMError(Exception):
    def __init__(self, status: int, body: dict | str):
        self.status, self.body = status, body
        super().__init__(f"LLM HTTP {status}: {str(body)[:300]}")


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over an AF_UNIX stream socket."""

    def __init__(self, sock_path: str, timeout: float = 600):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s


def _connection(url: str | None, unix_socket: str | None, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    if unix_socket:
        return UnixHTTPConnection(unix_socket, timeout), ""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=timeout, context=ssl.create_default_context())
    elif u.scheme == "http":
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    else:
        raise ValueError(f"unsupported URL scheme: {u.scheme!r}")
    return conn, u.path.rstrip("/")


def _parse(raw: bytes) -> dict | str:
    text_ = raw.decode("utf-8", "replace")
    try:
        return json.loads(text_)  # DeepSeek may send keep-alive blank lines first; json.loads skips them
    except ValueError:
        return text_[:2000]


def request_json(method: str, body: dict | None = None, *, url: str | None = None, unix_socket: str | None = None,
                 path: str = "/chat/completions", headers: dict | None = None, timeout: float = 600) -> dict:
    """One JSON request with retries on 429/500/503. Raises LLMError (status 0 = network error)."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(RETRIES + 1):
        conn, prefix = _connection(url, unix_socket, timeout)
        try:
            conn.request(method, prefix + path, body=data, headers=hdrs)
            resp = conn.getresponse()
            status, raw, retry_after = resp.status, resp.read(), resp.getheader("Retry-After")
        except (OSError, http.client.HTTPException) as e:
            raise LLMError(0, f"{type(e).__name__}: {e}") from None
        finally:
            conn.close()
        if 200 <= status < 300:
            parsed = _parse(raw)
            if not isinstance(parsed, dict):
                raise LLMError(status, parsed)
            return parsed
        if status in RETRY_STATUS and attempt < RETRIES:
            try:
                wait = min(float(retry_after), 30.0)
            except (TypeError, ValueError):
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            time.sleep(wait)
            continue
        raise LLMError(status, _parse(raw))
    raise AssertionError("unreachable")


def post_json(body: dict, *, url: str | None = None, unix_socket: str | None = None, path: str = "/chat/completions",
              headers: dict | None = None, timeout: float = 600) -> dict:
    """POST a JSON body to `url` + `path` (or to `path` on a unix socket) and return the JSON reply."""
    return request_json("POST", body, url=url, unix_socket=unix_socket, path=path, headers=headers, timeout=timeout)


def get_json(*, url: str | None = None, unix_socket: str | None = None, path: str = "/",
             headers: dict | None = None, timeout: float = 30) -> dict:
    return request_json("GET", None, url=url, unix_socket=unix_socket, path=path, headers=headers, timeout=timeout)


def _message(resp: dict) -> dict:
    try:
        return resp["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def assistant_message(resp: dict) -> dict:
    """The assistant message to append to the history (keeps reasoning_content and tool_calls)."""
    msg = _message(resp)
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("reasoning_content") is not None:
        out["reasoning_content"] = msg["reasoning_content"]
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {"id": c.get("id"), "type": c.get("type", "function"),
             "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
            for c in msg["tool_calls"]
        ]
    return out


def tool_calls(resp: dict) -> list[tuple[str, str, dict | str]]:
    """(id, name, args). args is the parsed JSON object, or the raw string when it does not parse."""
    calls = []
    for c in _message(resp).get("tool_calls") or []:
        fn = c.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = raw
        calls.append((c.get("id", ""), fn.get("name", ""), args))
    return calls


def text(resp: dict) -> str:
    return _message(resp).get("content") or ""


def finish_reason(resp: dict) -> str | None:
    try:
        return resp["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def usage_tokens(resp: dict) -> dict:
    """{hit, miss, out, reasoning} from a DeepSeek (or plain OpenAI) usage block."""
    u = resp.get("usage") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    if "prompt_cache_hit_tokens" in u or "prompt_cache_miss_tokens" in u:
        hit, miss = int(u.get("prompt_cache_hit_tokens") or 0), int(u.get("prompt_cache_miss_tokens") or 0)
    else:
        hit = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        miss = max(prompt - hit, 0)
    reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    return {"hit": hit, "miss": miss, "out": int(u.get("completion_tokens") or 0), "reasoning": reasoning}


def tool_schema(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}
