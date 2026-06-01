"""Real-mode AI client — TFY AI Gateway (OpenAI-compatible) -> AWS Bedrock fallback chain.

OWNER: Atlas. This module is fully implemented by the AI/Bedrock teammate. The placeholder
below documents the required interface so other modules can import it during the build.
"""
from __future__ import annotations
import deadman.config as config

RESILIENT_MODEL = config.TFY_RESILIENT_MODEL


def complete(prompt: str) -> dict:
    """One governed completion through the TFY AI Gateway virtual model.

    Returns: {"text": str, "served_by": str, "fallback_depth": int, "from_cache": bool, "raw": object}
    """
    raise NotImplementedError("realmode_ai.complete — implemented by Atlas")


def resolve_model_id(family: str, region: str) -> str:
    """Resolve the exact Bedrock modelId/inference-profile for a family hint at startup."""
    raise NotImplementedError("realmode_ai.resolve_model_id — implemented by Atlas")
