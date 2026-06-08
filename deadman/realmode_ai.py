"""Real-mode AI client — TFY AI Gateway (OpenAI-compatible) -> AWS Bedrock fallback chain.

OWNER: Atlas.

complete() calls the TFY AI Gateway virtual model (OpenAI-compatible endpoint) and
parses TrueFoundry's x-tfy-* response headers to derive fallback_depth, served_by, and
from_cache.  All header parsing is best-effort — missing headers never crash the caller.

resolve_model_id() uses boto3 at startup to resolve exact Bedrock modelId / inference-
profile ids from a family hint string, so we never ship a stale hardcoded ARN.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import deadman.config as config
from deadman.guardrails import GatewayGuardrailBlock

log = logging.getLogger(__name__)

# Substrings that mark a gateway response as a guardrail block rather than a transport
# failure. TFY's exact error shape may vary by tenant version, so detection is keyword +
# header based and deliberately defensive — same philosophy as the header parsing below.
_GUARDRAIL_MARKERS = (
    "guardrail",
    "prompt injection",
    "prompt-injection",
    "blocked by",
    "content blocked",
    "policy violation",
    "flagged",
    "input was rejected",
)
_GUARDRAIL_HEADERS = ("x-tfy-guardrail", "x-tfy-guardrail-action", "x-tfy-blocked")
_GUARDRAIL_HEADER_PASS_VALUES = {"", "none", "allow", "allowed", "pass", "passed", "ok"}

RESILIENT_MODEL = config.TFY_RESILIENT_MODEL

# Module-level cache for resolved model ids: (family, region) -> modelId string.
_resolved_ids: dict[tuple[str, str], str] = {}

# Map the served model string back to a tier index.  Built once from FALLBACK_CHAIN.
def _family_tier_map() -> dict[str, int]:
    return {entry["family"]: entry["tier"] for entry in config.FALLBACK_CHAIN}


def _depth_from_headers(headers: Any) -> int:
    """Parse fallback depth from TFY response headers.

    TrueFoundry surfaces routing decisions in several headers (documented as of June 2026):
      x-tfy-fallback-depth   — integer hop count (0 = primary, 1 = first fallback, ...)
      x-tfy-retry-count      — retry attempts consumed before success (not fallback depth)
      x-tfy-backend          — the model/backend that actually served the request
      x-tfy-target-id        — the target id from the gateway config that served the request
      x-tfy-cache            — "hit" when the semantic cache was used

    We prefer x-tfy-fallback-depth as the authoritative hop counter.  If absent we fall
    back to matching x-tfy-backend (or x-tfy-target-id) against the FALLBACK_CHAIN family
    hints or target ids.  Unknown served model -> depth 0 (safe default).

    NOTE: these header names are best-effort against TrueFoundry's publicly documented
    behaviour; the gateway spec may use slightly different capitalisation or field names in
    future tenant versions — the defensive try/except ensures we never crash on a header
    shape mismatch.
    """
    try:
        # Primary: explicit depth counter.
        depth_raw = _header(headers, "x-tfy-fallback-depth")
        if depth_raw is not None:
            return int(depth_raw)

        # Secondary: derive from the served backend/target id.
        backend = _header(headers, "x-tfy-backend") or _header(headers, "x-tfy-target-id") or ""
        if backend:
            fmap = _family_tier_map()
            for family, tier in fmap.items():
                if family in backend.lower():
                    return tier
            # target-id match (e.g. "claude-use1", "llama-usw2")
            target_tier_map = {entry["tier"]: entry for entry in config.FALLBACK_CHAIN}
            # Try to match numeric tier suffix in target id (e.g. "tier2-llama")
            for entry in config.FALLBACK_CHAIN:
                tid = f"tier{entry['tier']}"
                if tid in backend.lower():
                    return entry["tier"]

        return 0
    except Exception:  # noqa: BLE001
        log.debug("Could not parse fallback depth from headers; defaulting to 0", exc_info=True)
        return 0


def _served_by_from_headers(headers: Any, completion: Any) -> str:
    """Extract the backend model name that served this response."""
    try:
        backend = (
            _header(headers, "x-tfy-backend")
            or _header(headers, "x-tfy-target-id")
            or _header(headers, "x-tfy-model")
        )
        if backend:
            return backend
        # Fall back to the model field in the response itself.
        if hasattr(completion, "model") and completion.model:
            return completion.model
    except Exception:  # noqa: BLE001
        log.debug("Could not determine served_by; falling back to virtual model name", exc_info=True)
    return config.TFY_RESILIENT_MODEL


def _is_cache_hit(headers: Any) -> bool:
    """Return True when TFY signals a semantic cache hit.

    TrueFoundry uses 'x-tfy-cache: hit' (analogous to Cloudflare's CF-Cache-Status).
    Also check 'x-tfy-cache-status' as an alternative header name.
    """
    try:
        val = _header(headers, "x-tfy-cache") or _header(headers, "x-tfy-cache-status") or ""
        return val.strip().lower() == "hit"
    except Exception:  # noqa: BLE001
        return False


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup that works with both dict-like and httpx Header objects."""
    if headers is None:
        return None
    try:
        # httpx Headers / requests CaseInsensitiveDict — direct key access is CI.
        val = headers.get(name)
        if val is not None:
            return str(val)
        # Some response wrappers expose headers as a plain dict with lower keys.
        val = headers.get(name.lower())
        if val is not None:
            return str(val)
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_guardrail_violation(exc: Any) -> bool:
    """Best-effort: did the gateway reject this call at its guardrail layer?

    Distinguishes a guardrail block (e.g. block-prompt-injection in infra/guardrails.yaml)
    from an ordinary transport/availability failure, so the caller can degrade to a safe
    hold instead of blindly retrying hostile input. Inspects, in order:
      • an explicit x-tfy-guardrail* response header (non-pass value), then
      • guardrail keywords in the error message / JSON body.
    Never raises — an unrecognised error shape simply returns False (treated as transport).
    """
    try:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        for name in _GUARDRAIL_HEADERS:
            val = _header(headers, name)
            if val is not None and val.strip().lower() not in _GUARDRAIL_HEADER_PASS_VALUES:
                return True

        text = str(getattr(exc, "message", "") or exc).lower()
        body = getattr(exc, "body", None)
        if body is not None:
            try:
                text += " " + json.dumps(body, default=str).lower()
            except (TypeError, ValueError):
                text += " " + str(body).lower()
        return any(marker in text for marker in _GUARDRAIL_MARKERS)
    except Exception:  # noqa: BLE001 — detection must never mask the original error
        log.debug("guardrail-violation detection failed; treating as transport error", exc_info=True)
        return False


def complete(prompt: str) -> dict:
    """One governed completion through the TFY AI Gateway virtual model.

    Uses the OpenAI SDK pointed at the TFY gateway (OpenAI-compatible endpoint).
    Reads x-tfy-* response headers to populate fallback_depth, served_by, from_cache.

    Returns:
        {
          "text": str,
          "served_by": str,           # backend model / target that served the request
          "fallback_depth": int,      # 0=primary tier; increments per fallback hop
          "from_cache": bool,         # True when the semantic cache answered
          "raw": object               # the raw OpenAI ChatCompletion object
        }

    Raises:
        GatewayGuardrailBlock  — when the gateway blocks the call at its guardrail layer
                                 (e.g. prompt-injection detected); caller should degrade.
        openai.OpenAIError     — on hard gateway/transport failure (caller should catch)
    """
    config.require_ai_gateway_config()

    # Lazy import — keeps mock mode free of the openai dependency.
    import openai  # type: ignore[import]

    client = openai.OpenAI(
        api_key=config.TFY_API_KEY,
        base_url=config.TFY_GATEWAY_BASE_URL,
        default_headers={"x-tfy-metadata": config.TFY_METADATA},
    )

    # with_raw_response gives us access to response headers alongside the parsed body.
    # A gateway guardrail block surfaces as an OpenAI API error; translate it into a
    # typed GatewayGuardrailBlock so the commander handles it as a guardrail event, not
    # an outage. Genuine transport/availability errors are re-raised unchanged.
    try:
        raw_response = client.chat.completions.with_raw_response.create(
            model=config.TFY_RESILIENT_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
    except openai.OpenAIError as exc:
        if _is_guardrail_violation(exc):
            raise GatewayGuardrailBlock(
                f"TFY AI Gateway guardrail blocked the completion: {str(exc)[:300]}"
            ) from exc
        raise

    completion = raw_response.parse()
    headers = raw_response.headers  # httpx Headers object

    # --- parse routing metadata from headers (best-effort) ---
    from_cache = _is_cache_hit(headers)
    if from_cache:
        depth = config.SEMANTIC_CACHE_TIER
    else:
        depth = _depth_from_headers(headers)

    served_by = _served_by_from_headers(headers, completion)

    text = completion.choices[0].message.content or ""

    return {
        "text": text,
        "served_by": served_by,
        "fallback_depth": depth,
        "from_cache": from_cache,
        "raw": completion,
    }


def resolve_model_id(family: str, region: str) -> str:
    """Resolve the exact Bedrock modelId / inference-profile id for a family hint.

    Calls boto3 ListFoundationModels and ListInferenceProfiles in *region*.  Prefers
    inference profiles with a 'global.' or 'us.' prefix (cross-region resilience) when
    available.  Results are module-cached so the round-trip only happens once per
    (family, region) pair per process.

    On any AWS / boto3 error, or when no match is found, logs a warning and returns the
    best-known explicit modelId from config.FALLBACK_CHAIN for that family.  Never raises.
    """
    cache_key = (family, region)
    if cache_key in _resolved_ids:
        return _resolved_ids[cache_key]

    # Fallback from config in case boto3 is unavailable or no match is found.
    config_fallback = _best_known_id(family)

    try:
        import boto3  # type: ignore[import]

        bedrock = boto3.client("bedrock", region_name=region)

        # --- 1. Check inference profiles first (prefer global./us. prefixes) ---
        try:
            profiles_resp = bedrock.list_inference_profiles()
            profiles = profiles_resp.get("inferenceProfileSummaries", [])
            preferred: list[str] = []
            secondary: list[str] = []
            for p in profiles:
                pid = p.get("inferenceProfileId", "") or p.get("inferenceProfileArn", "")
                if family.lower() in pid.lower():
                    if pid.startswith("global.") or pid.startswith("us."):
                        preferred.append(pid)
                    else:
                        secondary.append(pid)
            if preferred:
                result = preferred[0]
                _resolved_ids[cache_key] = result
                log.info("Resolved %s/%s -> inference profile (preferred prefix): %s", family, region, result)
                return result
            if secondary:
                result = secondary[0]
                _resolved_ids[cache_key] = result
                log.info("Resolved %s/%s -> inference profile: %s", family, region, result)
                return result
        except Exception as exc:  # noqa: BLE001
            log.debug("list_inference_profiles failed for %s: %s", region, exc)

        # --- 2. Fall back to foundation models ---
        try:
            models_resp = bedrock.list_foundation_models()
            models = models_resp.get("modelSummaries", [])
            for m in models:
                mid = m.get("modelId", "")
                if family.lower() in mid.lower():
                    _resolved_ids[cache_key] = mid
                    log.info("Resolved %s/%s -> foundation model: %s", family, region, mid)
                    return mid
        except Exception as exc:  # noqa: BLE001
            log.debug("list_foundation_models failed for %s: %s", region, exc)

    except ImportError:
        log.warning("boto3 not available; using config fallback id for %s/%s: %s", family, region, config_fallback)
    except Exception as exc:  # noqa: BLE001
        log.warning("AWS resolution failed for %s/%s (%s); using config fallback: %s", family, region, exc, config_fallback)

    # Nothing found — use the best-known explicit id from config.
    log.info("No boto3 match for %s/%s; using config fallback: %s", family, region, config_fallback)
    _resolved_ids[cache_key] = config_fallback
    return config_fallback


def _best_known_id(family: str) -> str:
    """Return the best-known explicit modelId from config for the given family hint."""
    for entry in config.FALLBACK_CHAIN:
        if entry["family"] == family:
            return entry["model"]
    # If not found in chain, return the family hint itself as a last resort.
    return family
