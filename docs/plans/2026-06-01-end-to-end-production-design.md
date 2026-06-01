# DEADMAN → End-to-End Production: Design (iteration 2)

**Date:** 2026-06-01
**Branch:** `feat/end-to-end-production`
**Status:** Approved (scope confirmed via clarifying questions)

## Goal

Close the remaining gaps between "works + real-wired" and "complete, end-to-end,
production-ready." Builds on the current `main` (148 tests, real TFY/Bedrock wiring,
config validation + `readiness()`).

User decisions (2026-06-01):
- **Real agentic reasoning loop** (LLM actually diagnoses + selects tools), AND keep the
  deterministic scripted scenario as the demo path.
- Hardening tracks: **CI/CD, K8s+Helm+Terraform, Observability, Code-quality + ops-resilience** (all).
- **Deploy target: TrueFoundry platform** (lean into TFY deploy specs; Terraform for AWS
  resources; Helm chart as a portable secondary).

## Confirmed gaps (from analysis)

1. **Scripted commander** — `commander.py` hardcodes `PR-1337`/`prod-node-7` and discards
   `ai.complete()` output. The agent does not actually *decide*. (biggest "no-fake" gap)
2. **No CI/CD** — no `.github/workflows`.
3. **No deploy artifacts** — no TFY spec / Helm / Terraform.
4. **No code-quality tooling** — no ruff/mypy/pre-commit.
5. **No `/metrics`**, OTel `otel.py` exists but is not wired into the agent; `readiness()`
   exists but no `/readyz`.
6. **Ops gaps** — no graceful shutdown, no rate limiting, no incident lifecycle/TTL.

## Architecture additions

### Agentic loop (Cortex)
- `deadman/tools.py` — a **tool registry**: each tool = {name, scope, destructive?, json-schema,
  handler}. The MCPGateway executes by name; the registry is the single source of truth for
  what the agent can do (mock handlers for demo, real handlers via `realmode_mcp` in real mode).
- `deadman/planner.py` — parse the model's response into a structured **plan / tool-call**
  (OpenAI tool-calling format). Robust to malformed output (falls back to safe-hold).
- `deadman/commander.py` — `Deadman.run()` gains an **agentic loop**: `reason → select tool →
  guard+execute via MCP → observe result → repeat` until the incident is resolved or budget
  exhausted, with the durable state machine + exactly-once preserved. A `scenario=` parameter
  keeps the **deterministic demo** (fixed PR-1337 plan) for the stage demo and existing tests.
  Idempotency keys derive from the *chosen* action, not a hardcoded constant.

### Observability (Pulse)
- `deadman/metrics.py` — Prometheus counters/histograms (incidents, fallback_depth,
  double_executions, guardrail_blocks, tool latency). `/metrics` endpoint.
- `/readyz` wired to `config.readiness()`; `/healthz` stays liveness.
- `deadman/logging_config.py` — structured JSON logs + per-incident correlation id.
- OTel spans wrapping each agent step + tool call (via `otel.py` helpers).

### Ops resilience (Rampart)
- Graceful shutdown (drain in-flight incidents on SIGTERM).
- Token-bucket rate limiting on `/incident`.
- Incident **lifecycle** states (`triage→mitigating→resolved→closed`) + state TTL/cleanup
  (DynamoDB TTL attribute; file backend sweep).

### CI/CD + quality (Pipeline)
- `.github/workflows/ci.yml` — ruff, mypy, pytest, pip-audit + bandit, Docker build + Trivy.
- `pyproject.toml` (ruff + mypy config), `.pre-commit-config.yaml`, `requirements-dev.txt`.

### Deploy (Terra)
- `deploy/truefoundry/` — TFY Service deploy spec (image, port, env, autoscale, probes) +
  references to the existing `infra/ai_gateway.yaml` / `infra/guardrails.yaml`.
- `terraform/` — DynamoDB table (PK/SK + TTL + optional GSI) + IAM policy/role for Bedrock +
  DynamoDB least-privilege.
- `deploy/helm/` — portable Helm chart (Deployment, Service, HPA, PDB, probes, ConfigMap/Secret).

## Team (collision-free waves)

| Wave | Agent(s) | Files (disjoint within a wave) |
|---|---|---|
| A | **Cortex** | `tools.py`, `planner.py`, `commander.py` (agentic loop + demo path) |
| B (∥) | **Pipeline** / **Terra** | `.github/`, `pyproject.toml`, `.pre-commit-config.yaml`, `requirements-dev.txt` / `deploy/**`, `terraform/**` |
| C | **Pulse** | `metrics.py`, `logging_config.py`, `otel.py`, wires `webhook.py` + spans in `commander.py`/`mcp_gateway.py` |
| D | **Rampart** | `webhook.py` (rate-limit, shutdown), `state.py` (lifecycle/TTL), `lifecycle.py` |
| E | **Sentinel** | `tests/**` (agentic loop, metrics, lifecycle, integration vs dynamodb-local) |
| F | **Raven** 😈 | read-only red-team → lead fixes |

Lead lays interface contracts, integrates between waves, runs the final `ruff --fix`, and
re-runs Raven's adversarial probes.

## Invariants (must not regress)
- 148 existing tests stay green; `prove_exactly_once.py` PASS; demo prints NAIVE:1/DEADMAN:0.
- Mock mode runs with zero heavy deps; real-mode code stays correct against documented APIs.
- Exactly-once spine (claim_commit + live reconcile) unchanged.

## Verification gates
`ruff` clean, `mypy` clean (deadman/), full `pytest` green incl. new integration tests,
`/metrics` + `/readyz` respond, Terraform `validate`, Helm `lint`, TFY spec schema-valid,
CI workflow syntactically valid, Raven GO.
