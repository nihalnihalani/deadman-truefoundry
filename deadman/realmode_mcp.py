"""Real-mode MCP client — TFY MCP Gateway (Cedar default-deny + idempotency + OTel audit).

OWNER: Vault.

Wraps the TrueFoundry MCP Gateway REST API with:
  • Idempotency-Key header for exactly-once enforcement at the gateway layer.
  • HTTP 409 / body-flag detection as a skipped-idempotent signal.
  • Retry-with-backoff on 429 / 5xx (2 retries, exponential back-off).
  • Clear errors on non-retryable failures — never silently succeed.
  • Lazy `requests` import so mock-mode has zero extra dependencies.
"""
from __future__ import annotations
import time
import deadman.config as config


# ── Retry configuration ───────────────────────────────────────────────────────

_MAX_RETRIES = 2          # total extra attempts after the first try
_RETRY_BASE_DELAY = 1.0   # seconds; doubles each retry

# HTTP status codes that warrant a retry.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ── Public interface ──────────────────────────────────────────────────────────

class MCPGatewayError(Exception):
    """Raised when the MCP Gateway returns a non-retryable error response."""


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_body(resp):
    """Parse JSON if possible; fall back to raw text."""
    try:
        return resp.json()
    except Exception:
        return resp.text
