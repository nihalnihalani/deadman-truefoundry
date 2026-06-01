"""Real-mode clients — route through the TrueFoundry AI Gateway to AWS Bedrock.

The TrueFoundry AI Gateway is OpenAI-compatible, so the client is thin: you point the
OpenAI SDK at the gateway base URL and call a *virtual model* whose fallback chain
(Claude us-east-1 -> us-west-2 -> Llama -> Mistral -> Cohere -> semantic cache) is
configured declaratively in `infra/ai_gateway.yaml` and applied with `tfy apply`.
The gateway does the routing/fallback/retry/cache; your code stays simple, and the
response headers tell you which backend served the request + whether a fallback fired.

Requires: pip install openai ; and TFY_API_KEY + TFY_GATEWAY_BASE_URL in .env.
"""
from __future__ import annotations
import os


def _client():
    from openai import OpenAI  # imported lazily so mock mode needs no deps
    return OpenAI(
        api_key=os.environ["TFY_API_KEY"],
        base_url=os.environ["TFY_GATEWAY_BASE_URL"],  # e.g. https://<your>.truefoundry.cloud/api/llm
    )


# The "virtual model" name you configured in the AI Gateway with the fallback chain.
RESILIENT_MODEL = os.getenv("TFY_RESILIENT_MODEL", "deadman-resilient-bedrock")


def complete(prompt: str) -> dict:
    """One governed completion. The gateway handles fallback/retry/cache transparently."""
    resp = _client().chat.completions.create(
        model=RESILIENT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        extra_headers={"x-tfy-metadata": "app=deadman,role=incident-commander"},
    )
    # TrueFoundry returns routing info in response metadata/headers; surface what you can.
    served_by = getattr(resp, "model", RESILIENT_MODEL)
    return {"text": resp.choices[0].message.content, "served_by": served_by, "raw": resp}


# --- MCP Gateway tool call (real) ---------------------------------------------
# Tools are invoked through the MCP Gateway endpoint so they inherit scoped RBAC,
# Cedar/OPA default-deny at the Pre-Tool hook, Post-Tool result inspection, and the
# OpenTelemetry audit log. Wire your MCP Gateway URL + the per-tool idempotency key.
def call_tool(tool: str, args: dict, idempotency_key: str) -> dict:
    import requests  # lazy
    url = os.environ["TFY_MCP_GATEWAY_URL"].rstrip("/") + "/tools/" + tool
    headers = {
        "Authorization": f"Bearer {os.environ['TFY_API_KEY']}",
        "Idempotency-Key": idempotency_key,   # the gateway/your service enforces exactly-once
    }
    r = requests.post(url, json=args, headers=headers, timeout=30)
    return {"status_code": r.status_code, "body": r.json() if r.content else None}
