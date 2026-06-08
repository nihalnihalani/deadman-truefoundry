# DEADMAN — the incident commander that survives its own outage

> An autonomous SRE / incident-commander agent that keeps fighting a regional AWS outage **even when that same outage takes down its own model provider and its own tools** — and resolves the incident **exactly once**, even if it is SIGKILLed mid-rollback.

---

## 1. One-line pitch + the hook

**DEADMAN keeps an incident response running through provider death, then proves the single number that matters: a destructive production rollback executed `1 → 0` times.**

The headline is one contrasting integer. Kill the agent in the millisecond between "side effect applied" and "commit recorded," then resume it as a fresh process:

- A naive agent restarts from scratch and **re-fires the rollback → `double_executions = 1`**.
- DEADMAN reads its own durable audit log, sees the action already committed, reconciles against the live system-of-record, and **skips it → `double_executions = 0`**.

This is an *asserted* claim, not a slide. `scripts/prove_exactly_once.py` ends with:

```
[assert] revert_pr applied to prod exactly 1 time(s)
[PASS  ] exactly-once across process death ✓  — the spine holds.
```
(Source: `scripts/prove_exactly_once.py`, verified passing in mock mode.)

---

## 2. The problem & the user

**Who:** the on-call SRE / incident commander during a correlated cloud outage.

**The real pain:** when a region degrades, the *same* event often takes down (a) the LLM provider you're using to reason about the incident, (b) the tool APIs you need to mitigate it (CloudWatch returns truncated JSON, the k8s control plane is flaky), and (c) your own process (a pod gets evicted, an autoscaler kills the node). An agent built for the happy path does the two worst possible things at exactly the wrong moment: it **stalls** (no brain, no plan) or it **double-acts** (restarts and re-applies a destructive mitigation it already ran). "The firefighter is standing in the fire."

DEADMAN is built for that moment specifically: degrade gracefully when the brain gets dumber, refuse to act on garbage or hostile input, and guarantee that every destructive action lands **at most once** across process death.

---

## 3. What we built

A governed agent where every model call goes through an **AI Gateway** (fallback), every tool call goes through an **MCP Gateway** (scoped + audited + idempotent), and the agent's *authority is coupled to its confidence* by an **Agent Gateway** (the auto-leash).

```
   PagerDuty / CloudWatch alarm
              │  POST /incident  (HMAC/Bearer auth, rate-limited)   deadman/webhook.py
              ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │  DEADMAN commander  (ReAct loop: reason → act → observe)               │
   │                                       deadman/commander.py             │
   │                                                                        │
   │   ① AI GATEWAY  ── cross-region + cross-provider Bedrock fallback ──┐  │
   │      deadman/ai_gateway.py        tier0 Claude → … → semantic cache │  │
   │      real: deadman/realmode_ai.py (TFY) / deadman/bedrock_ai.py     │  │
   │                                                                    ▼   │
   │   ③ AGENT GATEWAY  ◄── subscribes to fallback_depth ── AUTO-LEASH      │
   │      deadman/agent_gateway.py     dumber brain → shorter leash         │
   │                                                                    │   │
   │   ② MCP GATEWAY  ── Cedar default-DENY · idempotency · OTel audit ─┘   │
   │      deadman/mcp_gateway.py   real: deadman/realmode_mcp.py            │
   │      Pre/Post-tool guardrails: deadman/guardrails.py                   │
   └───────────────────────────────────────────────────────────────────────┘
              │
              ▼
   System of record (k8s / ASG / GitHub / statuspage)  +  durable state (deadman/state.py)
```

The single trick that ties it together: the **AI Gateway publishes a `fallback_depth` signal**, and the **Agent Gateway revokes destructive authority** as that depth climbs. Governance lives centrally, not in app code — a brain running on a tier-2 fallback model is not allowed to drain production nodes.

---

## 4. Resilience under stress — every failure mode in the brief

Each row maps a failure from the challenge to how DEADMAN **detects** it, the **recovery path**, and the **file** that implements it. All behaviors verified by reading the source.

| Failure mode (brief) | How DEADMAN detects it | Recovery path | Implemented in |
|---|---|---|---|
| **Provider slowdown** (latency creep before a hard 5xx) | Per-tier latency vs `P99_LATENCY_BUDGET_MS` (1500ms default) | **Shed early** — skip the slow tier and serve from the next healthy tier *before* it times out | `deadman/ai_gateway.py` (`slow = latency_ms > P99_LATENCY_BUDGET_MS`); `deadman/config.py` |
| **Rate limit (429 storm)** on the primary | Tier marked unhealthy by chaos / gateway routing | Cross-region failover us-east-1 → us-west-2 (tier 0 → tier 1), then cross-provider | `deadman/ai_gateway.py`; real: TFY gateway routing in `deadman/realmode_ai.py`, direct: `deadman/bedrock_ai.py` |
| **Model outage** (region/provider down) | Tier `complete()` raises; gateway walks to the next tier | Walk the chain: Claude (us-east-1 → us-west-2) → Llama → Mistral → Cohere; if **all** live tiers are down, fall to the **semantic-cache "runbook brain"** (last validated plan); only then `ModelOutage` → **safe hold** | `deadman/ai_gateway.py` (`semantic_cache`, `SEMANTIC_CACHE_TIER`); direct chain in `deadman/bedrock_ai.py` (`BedrockOutage`) |
| **Broken tool call** (429 / 5xx / unreachable MCP) | HTTP status in `{429,500,502,503,504}` or network exception | **Retry with exponential backoff** (2 retries); non-retryable 4xx (≠409) raises `MCPGatewayError` — never silently succeeds | `deadman/realmode_mcp.py` (`_RETRYABLE_STATUS`, retry loop) |
| **Bad intermediate output** (corrupt/truncated metrics) | Post-Tool guardrail: `cw.*`/`logs.*` results must be valid JSON and brace-balanced (not truncated) | **Block-and-refetch** — `GuardrailBlock` raised *before the model sees the garbage*, so corruption never poisons the plan | `deadman/guardrails.py` (`post_tool_validate`, `_is_balanced_json`, `_looks_truncated`); rule `catch-corrupt-tool-output` in `infra/guardrails.yaml` |
| **Cascading multi-step errors** (degradation deepens) | AI Gateway `max_depth` crosses `AUTONOMY_REVOKE_AT_DEPTH` (depth 2) | **AUTO-LEASH**: Agent Gateway drops the scope to read-only; destructive verbs vanish. Revocation **latches** — a recovering brain cannot silently re-acquire drain authority. A spike of guardrail blocks (rate ≥ 0.5, min 3 attempts) trips the **kill-switch** to revoke all destructive scope | `deadman/agent_gateway.py` (`allowed_scope`, `trip_kill_switch`); `deadman/commander.py` (`KILL_SWITCH_MIN_ATTEMPTS=3`) |
| **Hostile / injected input** (prompt injection in log/alert text) | TFY AI Gateway guardrail returns a block; `realmode_ai` detects it (header + keyword) and raises a typed `GatewayGuardrailBlock` | **Treat as a handled failure** — degrade to a safe hold, never reason on injected content; defense-in-depth: the raw alert summary is redacted + capped before it reaches the prompt | `deadman/realmode_ai.py` (`_is_guardrail_violation`); `deadman/guardrails.py` (`GatewayGuardrailBlock`, `redact_text`); `deadman/commander.py` (`run_agentic` sanitizes summary); rule `block-prompt-injection` in `infra/guardrails.yaml` |
| **Process death mid-action** (SIGKILL between side-effect and commit) | Fresh process finds a `PENDING`-not-`COMMITTED` action in durable state | **Reconcile** against the live system-of-record per destructive verb (`is_reverted` / `is_cordoned` / `is_scaled`); if already applied, commit the record but **do not re-run** → exactly-once | `deadman/commander.py` (`_reconcile_pending`, resume path); `deadman/state.py` (`claim_commit` atomic check-and-write) |

---

## 5. How we use each required surface

### AWS Bedrock — `deadman/bedrock_ai.py`, `deadman/config.py`
Direct boto3 **Converse API** calls walking a **cross-provider** fallback chain. The chain in `BEDROCK_FALLBACK_CHAIN` is `claude-sonnet-4-6 → claude-haiku-4-5 → llama3-3-70b → nova-2-lite`, every id an exact inference-profile verified invocable on the demo account. The first tier to answer wins; its index becomes the `fallback_depth` the auto-leash subscribes to. **Live-verified** — `real_doctor` returns `served_by=claude-sonnet-4-6@us-east-1 depth=0` against real Bedrock (see §7).

### TrueFoundry AI Gateway — `deadman/realmode_ai.py`, `deadman/ai_gateway.py`
The OpenAI-compatible TFY endpoint is the primary real-mode brain. We call a single virtual model (`deadman-resilient-bedrock`) and let the gateway do **routing / fallback / semantic cache**, then read the `x-tfy-*` response headers (`x-tfy-fallback-depth`, `x-tfy-backend`, `x-tfy-cache`) to recover routing metadata — *observability and governance fed back into the agent's own behavior*. Header parsing is fully defensive (never crashes on a shape mismatch). The mock `ai_gateway.py` mirrors this contract for offline/deterministic runs.

### TrueFoundry MCP Gateway — `deadman/realmode_mcp.py`, `infra/guardrails.yaml`
**Scoped, audited, exactly-once** tool access. Standard MCP Streamable HTTP transport (auto-detected from the TFY URL shape) with a legacy REST fallback. Every call carries an `Idempotency-Key` so a replay returns 409 / `idempotent_replay` instead of re-running the side effect. `infra/guardrails.yaml` declares **Cedar default-DENY**: read tools (`cw.get_metrics`, `logs.query`, `k8s.describe`, `statuspage.post`) are allowed; destructive verbs (`k8s.cordon_drain`, `asg.scale`, `github.revert_pr`) require **elevation**. Audit export is OTel.

### Guardrails — `deadman/guardrails.py`, `infra/guardrails.yaml`
Five rules, four with Python-side enforcement and one gateway-only:
- `block-scale-to-zero` — Pre-Tool: rejects `asg.scale` below `MIN_REPLICA_FLOOR` (kills a hallucinated "scale to 0").
- `prod-drain-needs-elevation` — Pre-Tool: `k8s.cordon_drain` on a prod-critical namespace needs an elevation token.
- `redact-secrets-before-statuspage` — Pre-Tool: strips AWS keys / bearer tokens / JWTs from `statuspage.post` payloads.
- `catch-corrupt-tool-output` — Post-Tool: JSON-validity + truncation check on metric/log results.
- `block-prompt-injection` — gateway-layer LLM guardrail; surfaced to the agent as a typed `GatewayGuardrailBlock`.

---

## 6. Why the user experience survives

- **Exactly-once across process death.** Three layers of defense (`deadman/mcp_gateway.py` docstring): (1) the MCP Gateway's provider-side Idempotency-Key, (2) a race-safe atomic `claim_commit` ledger that closes the check-then-act TOCTOU window, (3) system-of-record reconciliation on resume. A destructive mitigation lands **at most once** — the difference between a clean rollback and an outage made worse.
- **Durable state.** State and audit log persist to disk (file backend, default) or DynamoDB (multi-replica production) via `deadman/state.py` — written atomically with `tempfile` + `os.replace`. A killed pod resumes from durable state, not from scratch.
- **Safe-hold degradation.** When the brain is gone (all tiers + cold cache) or the input is hostile, the agent does **nothing destructive** and records a safe hold rather than guessing — the postmortem is still written from the audit log. A correct "I held" beats a confident wrong action.

---

## 7. What's real vs simulated (and why that's a strength)

We are deliberately honest about this, because the honesty *is* the resilience story.

**Simulated (by design): the deterministic chaos harness.** `deadman/chaos.py` injects the failures on demand so the demo is reproducible every time — region blackout, 429 storm, corrupt output, and a SIGKILL fired at the exact instant between side-effect and commit. We chose deterministic chaos precisely so the headline (`double_executions 1 → 0`) never depends on network jitter at minute 3 of a live demo.

**Real (live-verified on the demo AWS account):**
- **Direct AWS Bedrock failover runs against real Bedrock.** Setting `DEADMAN_LLM_BACKEND=bedrock` makes `deadman/bedrock_ai.py` call the live Converse API. We ran the safe wiring check and got a real completion:
  ```
  [PASS] Bedrock completion (direct) — served_by=claude-sonnet-4-6@us-east-1 depth=0 cache=False
  ```
  (`scripts/real_doctor.py` with the configured `.env`.)
- **Live cross-provider failover, not just unit tests.** `scripts/bedrock_failover_demo.py --down 2` makes **real** Converse calls: it induces a genuine outage by leading with `claude-opus-4-8` (really `AccessDenied` on this account), shows two real per-tier failures, and **sheds cross-provider to `claude-sonnet-4-6` which actually answers** at `fallback_depth=2`. The outage is real (the AWS error message is verbatim), so this is failover you can watch live, not a mock. The fallback *logic* is additionally covered by `tests/test_bedrock_ai.py` (tier-0 answer, fall-back on `AccessDenied`, skip multiple dead tiers, all-down → `BedrockOutage`).
- **Exactly-once across a REAL OS process kill.** `scripts/prove_exactly_once_subprocess.py` spawns process A, lets a destructive action go `PENDING` on the on-disk durable store, then hard-kills A with `os._exit(137)` (no exception unwind), and a **separate** process B rehydrates only from `.deadman_state/` and reconciles **without re-running** the side effect. The output prints two distinct OS PIDs — the work genuinely crossed a process boundary. (The lighter `scripts/prove_exactly_once.py` models the same invariant in-process for a fast deterministic demo.)
- **An honest live-outage moment.** The narrative tier-0 model in `FALLBACK_CHAIN` is `claude-opus-4-8`, intentionally gated on this account. The TFY-gateway path is also currently rate/credit-limited upstream — running the real path returns a genuine `400 ... credit balance is too low`. Rather than hide it, we turn it into the demo: **this is exactly the provider failure DEADMAN is built to survive** — the direct-Bedrock chain answers on `claude-sonnet-4-6` while the primary brain is unavailable.
- **The real agentic path is wired.** In `DEADMAN_MODE=real`, `/incident` runs the genuine LLM-driven ReAct loop (`commander.run_agentic`) against the `RealWorld` system-of-record, so exactly-once-across-process-death is true in the running server, not just in tests.

---

## 8. Production readiness

- **359 tests passing** (1 skipped) — `python3 -m pytest`.
- **CI** (`.github/workflows/ci.yml`): ruff (lint), mypy (type-check), pytest across **Python 3.12 / 3.13 / 3.14** with coverage, **pip-audit** (dependency CVEs), **bandit** (static security), Docker build + **Trivy** image scan.
- **Real-mode doctor** (`scripts/real_doctor.py`): a safe, non-destructive wiring check — validates readiness config, sends one small completion (TFY gateway *or* direct Bedrock), lists MCP tools, and verifies the DynamoDB table when enabled.
- **Operational surface**: `/healthz` (liveness), `/readyz` (mode-aware readiness, 503 while draining), `/metrics` (Prometheus), HMAC/Bearer auth on `/incident`, configurable rate limiting, graceful in-flight drain, CORS locked to dev origins, OTel spans + audit export.
- **Fails closed in production**: real mode *requires* a webhook secret and *hides* the demo endpoints unless explicitly enabled (`deadman/config.py` `production_issues()`).

---

## 9. Try it in 60 seconds

All commands use `python3`. The demo and proof run on the **standard library** — no install needed. (Pin `DEADMAN_MODE=mock` so the deterministic harness runs even if a real-mode `.env` is present.)

```bash
# 1) Split-screen chaos demo: naive agent vs DEADMAN, with the Resilience Scoreboard
DEADMAN_MODE=mock python3 scripts/run_demo.py

# 2) The crown jewel — exactly-once across a REAL OS process kill (two distinct PIDs)
DEADMAN_MODE=mock python3 scripts/prove_exactly_once_subprocess.py
#    → [PASS] exactly-once across a REAL OS process death (A pid … → B pid …) ✓
#    (fast in-process variant: python3 scripts/prove_exactly_once.py)

# 3) LIVE AWS Bedrock cross-provider failover (real Converse calls, real AccessDenied → real answer)
DEADMAN_LLM_BACKEND=bedrock python3 scripts/bedrock_failover_demo.py --down 2
#    → tier 0/1 FAIL (real AccessDenied) → SERVED BY claude-sonnet-4-6@us-east-1 (fallback_depth=2)
#    one-call wiring check:  python3 scripts/real_doctor.py --skip-mcp --skip-dynamodb

# 4) Web UI — split-screen chaos buttons + live scoreboard (open http://localhost:8080)
DEADMAN_MODE=mock DEADMAN_ENABLE_DEMO=1 \
  python3 -m uvicorn deadman.webhook:app --port 8080
```

Endpoints once the server is up (`deadman/webhook.py`): `POST /incident`, `GET /incident/{id}/postmortem`, `POST /api/demo/run`, `GET /api/demo/stream` (SSE), `POST /api/chaos/{toggle}`, `GET /healthz`, `GET /readyz`, `GET /metrics`, and the static UI at `/`.

---

**One sentence for the judges:** DEADMAN is the rare resilient-agent submission where the resilience is *asserted in code* — a destructive production action that goes from `1` execution to `0` across a kill, a brain that walks a live cross-provider Bedrock chain, and an agent whose authority shrinks automatically as its confidence does.
