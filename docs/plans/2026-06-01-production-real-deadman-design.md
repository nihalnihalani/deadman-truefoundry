# DEADMAN → Production-Real: Design

**Date:** 2026-06-01
**Branch:** `feat/production-real-deadman`
**Status:** Approved

## Goal

Take the DEADMAN mock scaffold and make it a **complete, production-ready** project: fully
wire `DEADMAN_MODE=real` against the live **TrueFoundry AI Gateway + MCP Gateway + Agent
Gateway** and **AWS Bedrock**, with the *same agent logic* on both paths. No fake/mock data
in the real path. Keep mock mode (the deterministic stage demo) fully working.

User decisions (2026-06-01):
- **Real wiring** to TrueFoundry + Bedrock (real code, not simulated responses).
- **Keys later**: build correct against documented APIs + `.env` scaffolding; verify mock
  path now; user runs live verification once keys are present.
- **Primary brain: Claude Opus 4.8** (tier 0), cross-region twin at us-west-2.
- Scope: web scoreboard UI + full test suite + deployment configs + latest-model research.

## Research findings (June 2026)

The repo's model IDs are stale. Current Bedrock reality:
- Anthropic: **Claude Opus 4.8** (released May 28 2026, 1M ctx), Opus 4.7, **Sonnet 4.6**, **Haiku 4.5**.
- Meta: **Llama 4 Maverick / Scout** (1M ctx).
- Mistral: **Mistral Large 3**.
- Cohere: **Command R+ / Command A**.
- Newer Bedrock IDs use inference-profile prefixes (`global.` / `us.`). Some newest models
  lack ARN-versioned IDs → we resolve exact `modelId` strings at startup via
  `boto3 ListFoundationModels`/inference-profiles, never shipping a hardcoded deprecated ID.
- TrueFoundry AI Gateway is OpenAI-compatible: point OpenAI SDK at the dashboard `base_url`,
  model name format `{provider-account}/{model}` or a configured virtual model; fallback
  triggers on 401/403/404/429/5xx, 5-minute cooldown on unhealthy.
- Cedar Guardrails: default-deny at the **Pre-Tool** hook (`mcp_tool_pre_invoke_guardrails`).
- Agent Gateway: reverse proxy for agentic systems; policy enforcement + routing to MCP servers.

## New fallback chain

| Tier | Model | Region | Role |
|---|---|---|---|
| 0 | Claude Opus 4.8 | us-east-1 | primary brain |
| 1 | Claude Opus 4.8 | us-west-2 | cross-region failover (the literal outage) |
| 2 | Llama 4 Maverick | us-west-2 | cross-provider |
| 3 | Mistral Large 3 | us-west-2 | cross-provider |
| 4 | Cohere Command R+/A | us-west-2 | cross-provider |
| 5 | semantic cache | — | "runbook brain" last resort |

Exact `modelId` strings resolved live; the table is the intended ordering.

## Architecture (unchanged spine, real adapters added)

```
webhook /incident ─▶ Deadman commander (stateless worker)
        ├─ AIGateway  ── mock path | real path → realmode_ai → TFY AI Gateway → Bedrock
        ├─ MCPGateway ── mock path | real path → realmode_mcp → TFY MCP Gateway (Cedar + idempotency + OTel)
        ├─ AgentGateway ── fallback-depth → autonomy revoke
        └─ DurableState + AuditLog ── file backend | DynamoDB backend (exactly-once ledger)
```

The mock and real paths share the same `AIGateway`/`MCPGateway`/`Deadman` interfaces; only
the underlying client swaps based on `config.MODE`.

## Component ownership (team)

Shared foundation (laid by lead before fan-out): `config.py` model refresh + new config
keys; split `realmode.py` → `realmode_ai.py` + `realmode_mcp.py` (compat shim kept).

| Agent | Owns (disjoint files) |
|---|---|
| **Atlas** (AI/Bedrock) | `realmode_ai.py`, `ai_gateway.py` real path + header→fallback-depth parsing, live model resolution, `infra/ai_gateway.yaml` |
| **Vault** (MCP/Guardrails) | `realmode_mcp.py`, `mcp_gateway.py` real path, `guardrails.py`, `infra/guardrails.yaml` |
| **Anchor** (State/Commander) | `state.py` (file + DynamoDB backend), `commander.py` real run path decoupled from chaos, `agent_gateway.py`, `world.py` real adapters |
| **Forge** (DevOps) | `webhook.py` (real wiring + demo/chaos control + SSE), `otel.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `Makefile`, `.env.example` |
| **Beacon** (Frontend) | `web/` split-screen chaos UI + Resilience Scoreboard (SSE-driven) |
| **Sentinel** (QA) | `tests/` exactly-once property tests, kill/resume, guardrail, fallback, chaos, scope |
| **Raven** (😈 devil's advocate) | read-only red-team review of the integrated result |

## Interface contracts (stable across waves)

- `AIGateway.complete(prompt) -> Completion(text, backend, tier, from_cache)`; `.max_depth: int`.
- `MCPGateway.execute(tool, args, key, allowed_scope) -> ToolResult`; raises
  `GuardrailBlock | ScopeDenied | KillSignal`.
- `DurableState`: `.pending`, `set_pending`, `commit`, `note`. `AuditLog`: `write`,
  `is_committed`, `pending_keys`, `postmortem`.
- `AgentGateway.allowed_scope(fallback_depth) -> set`; `.drain_authority`.
- `Deadman(incident_id, world, chaos).run(resume=False) -> Scoreboard`.
- Real mode MUST NOT depend on `chaos`; chaos toggles are demo-only.

## Error handling

- AI: exhaust tiers → semantic cache → `ModelOutage` (safe-hold). Real path surfaces
  gateway fallback depth from response metadata/headers.
- MCP: Cedar default-deny → `ScopeDenied`; bad args → `GuardrailBlock` (pre); corrupt
  result → `GuardrailBlock` (post, refetch). Idempotency-Key enforces exactly-once.
- State: append-only; rehydrate-and-dedupe on resume; reconcile against system-of-record
  before re-acting on a PENDING-not-COMMITTED action.

## Testing

`pytest` suite: exactly-once across kill (property + scenario), resume reconciliation,
Cedar deny, pre/post guardrails, full fallback chain incl. cache, latency-shed, Agent
Gateway auto-leash, naive double-execution contrast, webhook API. Real-mode clients tested
with mocked transports (no live calls in CI). Target: green `pytest` + both demo scripts.

## Verification gates

1. `python scripts/prove_exactly_once.py` → PASS (exactly-once holds).
2. `python scripts/run_demo.py` → NAIVE 1 / DEADMAN 0.
3. `pytest` → all green.
4. Web UI renders the split-screen scoreboard and updates live.
5. Real-mode code imports + unit-tests green with mocked transports; live verification
   deferred to user's keys.
