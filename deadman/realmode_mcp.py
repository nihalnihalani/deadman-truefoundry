"""Real-mode MCP client — TFY MCP Gateway (Cedar default-deny + idempotency + OTel audit).

OWNER: Vault.

Wraps the TrueFoundry MCP Gateway with:
  • Standard MCP Streamable HTTP transport for URLs copied from the TFY MCP UI
    (for example https://gateway.truefoundry.ai/mcp/<server>/server).
  • Legacy REST /tools/{tool} transport for local stubs and older gateway shims.
  • Idempotency-Key header for exactly-once enforcement at the gateway layer.
  • HTTP 409 / body-flag detection as a skipped-idempotent signal.
  • Retry-with-backoff on 429 / 5xx (2 retries, exponential back-off).
  • Clear errors on non-retryable failures — never silently succeed.
  • Lazy imports so mock-mode has zero extra dependencies.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from urllib.parse import urlparse

import deadman.config as config


# ── Retry configuration ───────────────────────────────────────────────────────

_MAX_RETRIES = 2          # total extra attempts after the first try
_RETRY_BASE_DELAY = 1.0   # seconds; doubles each retry

# HTTP status codes that warrant a retry.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ── Public interface ──────────────────────────────────────────────────────────

class MCPGatewayError(Exception):
    """Raised when the MCP Gateway returns a non-retryable error response."""


def selected_transport() -> str:
    """Return the concrete transport for the configured MCP gateway URL.

    ``TFY_MCP_TRANSPORT=auto`` chooses the standard MCP transport when the URL
    looks like a TrueFoundry MCP server endpoint copied from the UI, and falls
    back to the legacy REST adapter otherwise. This keeps the old tests/local
    shims working while making the documented TFY URL shape the happy path.
    """
    transport = config.TFY_MCP_TRANSPORT
    if transport != "auto":
        return transport

    parsed = urlparse(config.TFY_MCP_GATEWAY_URL)
    path = parsed.path.rstrip("/")
    if "/mcp/" in path or path.endswith("/server") or path.endswith("/mcp"):
        return "mcp"
    return "rest"


def call_tool(tool: str, args: dict, idempotency_key: str) -> dict:
    """Invoke an MCP tool through the TFY MCP Gateway with exactly-once idempotency.

    Parameters
    ----------
    tool:
        Dotted tool name, e.g. ``"asg.scale"`` or ``"github.revert_pr"``.
    args:
        JSON-serialisable dict forwarded as the request body.
    idempotency_key:
        Stable, unique key for this specific call.  Passed via the
        ``Idempotency-Key`` HTTP header.  The gateway uses this to enforce
        exactly-once execution: a replayed request with the same key returns
        a 409 or a ``{"idempotent_replay": true}`` body instead of re-running
        the side effect.

    Returns
    -------
    dict with keys:
        ``status_code`` (int), ``body`` (parsed JSON or raw text),
        ``skipped_idempotent`` (bool).

    Raises
    ------
    MCPGatewayError
        After exhausting retries on retryable failures, or immediately on
        non-retryable failures (4xx other than 409).  Never silently succeeds.
    """
    config.require_mcp_gateway_config()

    if selected_transport() == "mcp":
        return _call_tool_mcp(tool, args, idempotency_key)
    return _call_tool_rest(tool, args, idempotency_key)


def list_tools() -> list[dict]:
    """List tools exposed by the configured MCP Gateway without running any tool.

    Used by the real-mode doctor command. The standard MCP path calls
    ``client.list_tools()``; the legacy REST path tries ``GET /tools``.
    """
    config.require_mcp_gateway_config()
    if selected_transport() == "mcp":
        return _list_tools_mcp()
    return _list_tools_rest()


# ── Legacy REST transport ────────────────────────────────────────────────────

def _call_tool_rest(tool: str, args: dict, idempotency_key: str) -> dict:
    # Lazy import — requests is only needed in real mode; mock mode has zero deps.
    import requests  # noqa: PLC0415

    url = config.TFY_MCP_GATEWAY_URL.rstrip("/") + "/tools/" + tool
    headers = {
        "Authorization": f"Bearer {config.TFY_API_KEY}",
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }

    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            time.sleep(delay)

        try:
            resp = requests.post(url, json=args, headers=headers, timeout=30)
        except requests.exceptions.RequestException as exc:
            # Network-level error (timeout, connection refused, etc.).
            last_exc = exc
            continue  # retry

        last_status = resp.status_code

        # ── Idempotent replay (exactly-once already handled by the gateway) ──
        if resp.status_code == 409:
            return {
                "status_code": resp.status_code,
                "body": _parse_body(resp),
                "skipped_idempotent": True,
            }

        # ── Successful response ──────────────────────────────────────────────
        if resp.status_code < 300:
            body = _parse_body(resp)
            # Some gateway versions signal a replay in the body rather than via
            # HTTP 409 (e.g. {"idempotent_replay": true}).
            skipped = isinstance(body, dict) and bool(body.get("idempotent_replay"))
            return {
                "status_code": resp.status_code,
                "body": body,
                "skipped_idempotent": skipped,
            }

        # ── Retryable server-side errors ─────────────────────────────────────
        if resp.status_code in _RETRYABLE_STATUS:
            last_exc = None  # clear any previous network exc
            continue  # will retry (if attempts remain)

        # ── Non-retryable client / server error ──────────────────────────────
        raise MCPGatewayError(
            f"MCP Gateway returned non-retryable HTTP {resp.status_code} "
            f"for tool={tool!r} key={idempotency_key!r}: {resp.text[:500]}"
        )

    # Exhausted all retries.
    if last_exc is not None:
        raise MCPGatewayError(
            f"MCP Gateway unreachable after {_MAX_RETRIES + 1} attempts "
            f"for tool={tool!r} key={idempotency_key!r}: {last_exc}"
        ) from last_exc

    raise MCPGatewayError(
        f"MCP Gateway returned HTTP {last_status} after {_MAX_RETRIES + 1} attempts "
        f"for tool={tool!r} key={idempotency_key!r}"
    )


def _list_tools_rest() -> list[dict]:
    import requests  # noqa: PLC0415

    url = config.TFY_MCP_GATEWAY_URL.rstrip("/") + "/tools"
    headers = {"Authorization": f"Bearer {config.TFY_API_KEY}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code >= 300:
        raise MCPGatewayError(
            f"MCP Gateway returned HTTP {resp.status_code} while listing tools: "
            f"{resp.text[:500]}"
        )
    body = _parse_body(resp)
    return _normalize_tool_list(body)


# ── Standard MCP Streamable HTTP transport ───────────────────────────────────

def _call_tool_mcp(tool: str, args: dict, idempotency_key: str) -> dict:
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            time.sleep(delay)

        try:
            return _run_coro(_call_tool_mcp_once(tool, args, idempotency_key))
        except Exception as exc:  # noqa: BLE001
            status = _status_code_from_exc(exc)
            last_status = status
            if status == 409:
                return {
                    "status_code": 409,
                    "body": _body_from_exc(exc),
                    "skipped_idempotent": True,
                }
            if status is None or status in _RETRYABLE_STATUS:
                last_exc = exc
                continue
            raise MCPGatewayError(
                f"MCP Gateway returned non-retryable HTTP {status} "
                f"for tool={tool!r} key={idempotency_key!r}: {str(exc)[:500]}"
            ) from exc

    if last_exc is not None:
        raise MCPGatewayError(
            f"MCP Gateway unreachable after {_MAX_RETRIES + 1} attempts "
            f"for tool={tool!r} key={idempotency_key!r}: {last_exc}"
        ) from last_exc

    raise MCPGatewayError(
        f"MCP Gateway returned HTTP {last_status} after {_MAX_RETRIES + 1} attempts "
        f"for tool={tool!r} key={idempotency_key!r}"
    )


async def _call_tool_mcp_once(tool: str, args: dict, idempotency_key: str) -> dict:
    try:
        from fastmcp import Client  # type: ignore[import]
        from fastmcp.client.transports import StreamableHttpTransport  # type: ignore[import]
    except ImportError as exc:
        raise MCPGatewayError(
            "TFY_MCP_TRANSPORT=mcp requires the optional 'fastmcp' package. "
            "Install project requirements or set TFY_MCP_TRANSPORT=rest for a REST shim."
        ) from exc

    transport = StreamableHttpTransport(
        url=config.TFY_MCP_GATEWAY_URL,
        headers={
            "Authorization": f"Bearer {config.TFY_API_KEY}",
            "Idempotency-Key": idempotency_key,
            "x-deadman-idempotency-key": idempotency_key,
        },
    )

    async with Client(transport) as client:
        result = await client.call_tool(tool, args)

    body = _mcp_result_to_body(result)
    return {
        "status_code": 200,
        "body": body,
        "skipped_idempotent": _body_signals_idempotent(body),
    }


def _list_tools_mcp() -> list[dict]:
    return _run_coro(_list_tools_mcp_once())


async def _list_tools_mcp_once() -> list[dict]:
    try:
        from fastmcp import Client  # type: ignore[import]
        from fastmcp.client.transports import StreamableHttpTransport  # type: ignore[import]
    except ImportError as exc:
        raise MCPGatewayError(
            "TFY_MCP_TRANSPORT=mcp requires the optional 'fastmcp' package. "
            "Install project requirements or set TFY_MCP_TRANSPORT=rest for a REST shim."
        ) from exc

    transport = StreamableHttpTransport(
        url=config.TFY_MCP_GATEWAY_URL,
        headers={"Authorization": f"Bearer {config.TFY_API_KEY}"},
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
    return _normalize_tool_list(tools)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_body(resp):
    """Parse JSON if possible; fall back to raw text."""
    try:
        return resp.json()
    except Exception:
        return resp.text


def _run_coro(coro):
    """Run an async MCP operation from sync agent code.

    The webhook offloads agent work to a worker thread, so the usual path is a
    simple ``asyncio.run``. The thread fallback makes direct sync calls robust
    even if a caller already has an event loop running.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, object] = {}

    def _runner():
        try:
            box["result"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("result")


def _mcp_result_to_body(result):
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return result.structured_content
    if hasattr(result, "data") and result.data is not None:
        return result.data
    if hasattr(result, "content"):
        return _content_to_body(result.content)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result


def _content_to_body(content):
    if isinstance(content, list):
        values = [_single_content_to_body(item) for item in content]
        return values[0] if len(values) == 1 else values
    return _single_content_to_body(content)


def _single_content_to_body(item):
    text = getattr(item, "text", None)
    if text is not None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return item


def _body_signals_idempotent(body) -> bool:
    if not isinstance(body, dict):
        return False
    return bool(
        body.get("idempotent_replay")
        or body.get("skipped_idempotent")
        or body.get("already_committed")
        or body.get("status") == "SKIPPED_IDEMPOTENT"
    )


def _normalize_tool_list(body) -> list[dict]:
    if hasattr(body, "tools"):
        body = body.tools
    if isinstance(body, dict):
        raw_tools = body.get("tools", body.get("data", body.get("result", [])))
    else:
        raw_tools = body
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list):
        raw_tools = list(raw_tools)

    tools: list[dict] = []
    for item in raw_tools:
        if isinstance(item, dict):
            tools.append(item)
        elif hasattr(item, "model_dump"):
            tools.append(item.model_dump())
        else:
            tools.append({
                "name": getattr(item, "name", str(item)),
                "description": getattr(item, "description", ""),
            })
    return tools


def _status_code_from_exc(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _body_from_exc(exc: Exception):
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        return response.json()
    except Exception:
        return getattr(response, "text", str(exc))
