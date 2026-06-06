# 💀 DEADMAN — the agent that survives its own outage

> **TrueFoundry "Resilient Agents" Hackathon** (with AWS Bedrock) · June 1–7, 2026
> An autonomous **SRE / incident-commander** that keeps fighting a regional AWS outage **even when that same outage takes down its own model provider and tools.** The firefighter is standing in the fire — and TrueFoundry is the literal reason it's still alive at step 38.

```
>> DOUBLE-EXECUTIONS   NAIVE: 1              DEADMAN: 0
```
That one number is the whole pitch. **It already runs** — see [Quickstart](#-quickstart-runs-on-the-stdlib-no-setup).

---

## ⚡ Quickstart (runs on the stdlib, no setup)
```bash
git clone <this-repo> deadman-truefoundry && cd deadman-truefoundry

# Day-1 crown jewel: kill the agent mid-rollback, resume, ASSERT exactly-once
python scripts/prove_exactly_once.py

# The split-screen chaos demo: naive agent vs DEADMAN + the Resilience Scoreboard
python scripts/run_demo.py

# Full local readiness gate: pytest + exactly-once proof + demo + compose config
make check
```
No API keys, no install — the mock gateways + a file-backed durable store let the full
**detect → fall back → guardrail → kill → resume exactly-once** loop run today. Set
`DEADMAN_MODE=real` + `.env` to route through the real TrueFoundry AI Gateway + MCP Gateway + Bedrock.

---

## 🏆 Why this project will win — every reason

### 1. It's the only idea whose failure domain *overlaps its workload*
Most "resilient agent" submissions are a normal agent with `fallback: true` bolted on. DEADMAN is the rare case where **resilience IS the product**: an incident-commander is, by definition, running *during* the outage — so rate limits, provider death, tool failures, and bad intermediate outputs aren't injected gimmicks, they're the actual operating environment. *The thing fighting the fire is standing in the fire.* No other domain makes the theme this intrinsic.

### 2. The judges ARE the user
Judges are **Nikunj Bajaj (CEO, TrueFoundry)**, **Preethi Kumaresan (AWS)**, and a principal applied scientist. On-call SREs at an AWS shop during a regional event is *their* world — and it maps onto the **literal May 7–8, 2026 us-east-1 outage**. Maximum "Usefulness" + "Potential Impact" before a line of the pitch.

### 3. It hits all 6 official criteria, deeply — scored 57/60
| Criterion | Score | How DEADMAN nails it |
|---|:--:|---|
| **AI Gateway** | 10 | 5-tier Bedrock fallback: Claude **us-east-1 → us-west-2** (cross-region, the literal outage) → **Llama → Mistral → Cohere** (cross-provider) → **semantic cache "runbook brain."** Latency-shed *before* a hard 5xx. Every tier tagged in the trace. |
| **MCP Gateway** | 10 | The audit log is load-bearing **three ways**: recovery ledger + **exactly-once dedup** + auto-postmortem. Cedar **default-DENY** on destructive verbs. |
| **Guardrails** | 9 | **Pre-Tool** validates destructive args (rejects `scale-to-0` below the replica floor); **Post-Tool** catches corrupt/truncated tool output before the model reasons on it — the literal "bad intermediate output" cascade-breaker. |
| **Resilience** | 10 | Append-only incident state machine **outside the provider** + idempotency-keyed actions → **rehydrate-and-dedupe on a kill mid-mitigation.** The rarest pattern (state loss on provider death), in the one domain where it's intrinsic. |
| **Usefulness** | 9 | Real, high-stakes user; a mid-task crash = a **double-executed destructive rollback**. |
| **Demo clarity** | 9 | A one-button "Correlated Blackout" cold open makes the thesis the first thing *seen*. |

### 4. The most TrueFoundry-native idea in the bracket (the WOW the CEO is fishing for)
**Authority degrades in lockstep with confidence.** DEADMAN couples the brand-new (May 27, 2026) **Agent Gateway** to the AI Gateway's fallback-depth signal: when the brain falls back to a weaker model, the Agent Gateway **automatically revokes the agent's `cordon_drain` / `revert_pr` authority** — a dumber brain gets a shorter leash, governed centrally, not in app code. You can watch `Drain authority` flip **ON → OFF** on the scoreboard. No other team will show this.

### 5. The single, irrefutable WOW moment
Kill the agent mid-rollback. The **naive agent restarts from step 1 and re-fires the destructive rollback (double-execution)**; DEADMAN reads its own audit log, sees the action already committed, **skips it**, and resumes. The scoreboard prints **Double-executions — NAIVE: 1 · DEADMAN: 0.** A single contrasting integer is more persuasive than any narration — and it proves state preservation + exactly-once + cross-Bedrock failover simultaneously.

### 6. Deterministic demo = zero stage risk
Every failure is a button; every recovery is reproducible. (We deliberately beat the flashier *voice* finalist precisely because a live sub-second voice failover can stutter once on camera and kill the thesis — DEADMAN's chaos never depends on network jitter at minute 3.)

### 7. Bedrock depth the AWS judge will actually credit
Cross-**region** (us-east-1→us-west-2) **and** cross-**provider** (Anthropic→Meta→Mistral→Cohere) failover, all on Bedrock, each hop visible in the trace. Not "I enabled fallbacks" — a real, tiered, observable failover story.

### 8. Built-in path to the $1k "best social media" prize
Two of the most screenshot-able artifacts in the whole hackathon: the *"we killed it at step 38 and it knew what it had already done"* clip, and the *"a dumber agent gets a shorter leash, automatically"* auto-leash clip. Daily build-in-public thread tagging @truefoundry.

---

## 🏗️ Architecture
```
 Incident webhook ─▶ DEADMAN COMMANDER (stateless worker; NO state held in-process)
                          │
        ┌─────────────────┼──────────────────────┐
        ▼                 ▼                       ▼
  TFY AI GATEWAY     TFY MCP GATEWAY          TFY AGENT GATEWAY (new, May 27)
  fallback chain     scoped tools + Cedar     autonomy budget COUPLED to
  latency-shed       default-DENY + Pre/Post  fallback-depth → revokes
  semantic cache     guardrails + OTel audit  destructive authority on degrade
        │                 │
  Bedrock 5 tiers    External State Store (DynamoDB / file) + Audit Log
                     = recovery ledger + exactly-once + postmortem
```

### Code map (this repo)
| File | Role |
|---|---|
| `deadman/ai_gateway.py` | **AI Gateway** — fallback chain, latency-shed, semantic-cache runbook brain, fallback-depth signal (mock + real paths) |
| `deadman/realmode_ai.py` | Real **AI Gateway** client — OpenAI-compatible TFY endpoint, `x-tfy-*` header→fallback-depth parsing, boto3 model-id resolution |
| `deadman/mcp_gateway.py` | **MCP Gateway + Guardrails** — Cedar default-deny, Pre/Post-Tool hooks, **idempotency = exactly-once**, audit (mock + real paths) |
| `deadman/realmode_mcp.py` | Real **MCP Gateway** client — `/tools/{tool}` with `Idempotency-Key`, retry/backoff |
| `deadman/guardrails.py` | Pure Pre/Post-Tool validators (single source of truth, shared by both paths) |
| `deadman/agent_gateway.py` | **Agent Gateway** — autonomy budget; latching revoke of destructive scope as the brain degrades + kill-switch |
| `deadman/state.py` | **Durable state + audit log** — pluggable `FileBackend` / `DynamoDBBackend`, race-safe `claim_commit` — the crown jewel |
| `deadman/world.py` | `World` (mock system of record) + `RealWorld` (queries the live provider for reconciliation) |
| `deadman/commander.py` | `NaiveAgent` vs `Deadman` (the resumable commander; runs with `chaos=None` in production) |
| `deadman/webhook.py` | FastAPI entrypoint — `/incident` (auth-gated), demo/SSE/chaos API, static UI mount, OTel |
| `deadman/otel.py` | Lazy OpenTelemetry init (no-op when unconfigured) |
| `deadman/chaos.py` | The chaos injector — every failure is a toggle (demo only) |
| `web/` | The split-screen chaos UI + live Resilience Scoreboard (vanilla JS, no build step) |
| `tests/` | Pytest suite (exactly-once, guardrails, fallback, auto-leash, webhook, production readiness, real clients) |
| `scripts/prove_exactly_once.py` | **Day-1 gate:** kill mid-rollback, resume, assert exactly-once |
| `scripts/run_demo.py` | The split-screen chaos demo + Resilience Scoreboard |

### The Bedrock fallback chain (tag each tier in the trace)
`Claude Opus 4.8 @ us-east-1` → `Claude Opus 4.8 @ us-west-2` → `Llama 4 Maverick` → `Mistral Large 3` → `Cohere Command R+` → `semantic cache`. Fallback on 401/403/404/429/5xx; **latency-shed when p99 breaches budget**. The exact Bedrock `modelId` strings (which carry `global.`/`us.` inference-profile prefixes and change as models are deprecated) are resolved at startup via `boto3 ListFoundationModels` / inference-profiles — see `deadman/realmode_ai.resolve_model_id` — so we never ship a stale hardcoded ARN.

---

## 🎬 The 4-minute chaos demo (build to this)
Split-screen. LEFT = naive (raw Bedrock, in-process state). RIGHT = DEADMAN. Bottom = Resilience Scoreboard.

| Time | Beat | Failure | LEFT | RIGHT |
|---|---|---|---|---|
| 0:00 | **Cold open — Correlated Blackout** | one "us-east-1 EVENT" button | starts dying in its own incident | *"the firefighter is in the fire"* — keeps commanding |
| 0:30 | 429 storm + regional outage | us-east-1 down | stalls | latency-shed → us-west-2 |
| 1:15 | All-Bedrock down | every live tier | dead | Llama→Mistral→Cohere→**cache**; **Agent Gateway revokes drain authority ON-SCREEN** |
| 2:00 | Corrupt intermediate output | garbage JSON | reasons on garbage | **Post-Tool guardrail** catches it |
| **2:45** | **🎯 KILL mid-rollback** | SIGKILL between side effect + commit | re-fires rollback → **DOUBLE** | reads its own audit log → **skips** → resumes |
| 3:30 | Recovery + postmortem | restore | still dead | auto-writes postmortem from the audit log |

---

## 🚀 Real mode (TrueFoundry + Bedrock)
Ready-to-edit configs ship in [`infra/`](./infra):
1. **AI Gateway** — apply the 5-tier Bedrock fallback + latency-shed + semantic cache:
   `tfy apply -f infra/ai_gateway.yaml` → exposes one virtual model `deadman-resilient-bedrock`.
2. **MCP Gateway + Guardrails** — `tfy apply -f infra/guardrails.yaml` sets Cedar default-deny on
   `k8s.cordon_drain`/`asg.scale`/`github.revert_pr`, the Pre/Post-Tool guardrails, the OTel audit
   log, and the **Agent Gateway** autonomy-budget coupling (revoke destructive scope at fallback depth 2).
3. Copy `.env.example` to `.env`, set `DEADMAN_MODE=real`, and fill:
   - `TFY_API_KEY`
   - `TFY_GATEWAY_BASE_URL` from **AI Gateway → Playground → Code** (OpenAI-compatible base URL)
   - `TFY_MCP_GATEWAY_URL` from **MCP Gateway → your server → Connect** (usually
     `https://gateway.truefoundry.ai/mcp/<server>/server`)
   - `DEADMAN_WEBHOOK_SECRET`
   `deadman/ai_gateway.py` then calls the
   OpenAI-compatible gateway via `deadman/realmode_ai.py` (it reads the `x-tfy-*` response headers
   to recover the fallback depth that drives the auto-leash); tools route through
   `deadman/realmode_mcp.py` → the MCP Gateway with an `Idempotency-Key` header. `TFY_MCP_TRANSPORT=auto`
   selects the standard MCP transport for the TrueFoundry server URL and keeps a REST shim for local tests.
4. Run the safe live wiring check. It performs one small model call and lists MCP tools, but never
   invokes destructive tools:
   `python scripts/real_doctor.py`
5. Run the webhook a PagerDuty/CloudWatch alarm hits: `uvicorn deadman.webhook:app --port 8080`.
   Set `DEADMAN_WEBHOOK_SECRET` to require a bearer token / HMAC signature on `/incident`, and
   leave `DEADMAN_ENABLE_DEMO` unset (or set it to `0`) so demo + chaos endpoints stay disabled
   in real mode. Check `/readyz` before routing real alerts; it returns 503 until required real
   mode config is present and unsafe demo/auth settings are closed.

The mock and real paths share the same agent logic — only the gateway clients swap.

### If you do not have an MCP server yet

Use the safe demo server in [`mcp_servers/deadman_safe_tools.py`](./mcp_servers/deadman_safe_tools.py)
to get real TrueFoundry MCP Gateway auth/audit/tool routing without touching production systems.
In the TrueFoundry **Add new MCP Server** screen, choose **Create a Hosted STDIO-based MCP Server**
for the hackathon/demo path and configure:

| Field | Value |
|---|---|
| Name | `deadman-tools` |
| Command | `python` |
| Args | `mcp_servers/deadman_safe_tools.py` |
| Auth | No auth / gateway-auth only |

After it is created, open the server's **How To Use** / **Connect** tab and copy the URL that looks
like `https://gateway.truefoundry.ai/mcp/deadman-tools/server`. Put that in `.env` as
`TFY_MCP_GATEWAY_URL`. For production, replace this safe stub with official/remote MCP servers for
GitHub, Kubernetes, CloudWatch, ASG, and Statuspage, with least-privilege auth.

### Production readiness checklist

Before deploying against live systems:
1. Copy `.env.example` to `.env` and set `DEADMAN_MODE=real`, `TFY_API_KEY`,
   `TFY_GATEWAY_BASE_URL`, `TFY_MCP_GATEWAY_URL`, and `DEADMAN_WEBHOOK_SECRET`.
2. Set `DEADMAN_ENABLE_DEMO=0` for production. In real mode the default is disabled, but making
   it explicit avoids operator ambiguity.
3. Use `DEADMAN_STATE_BACKEND=dynamodb` for multi-replica production. The file backend is durable
   across process death on one host, but not a distributed store.
4. Run `make check` locally, then `make real-doctor` with real credentials. Start the service and
   verify `GET /readyz` returns `{"ok": true, ...}`.
5. Apply and tenant-validate `infra/ai_gateway.yaml` and `infra/guardrails.yaml` because hosted
   gateway schemas can differ by TrueFoundry tenant/version.

### Exactly-once: the honest model
The headline claim — *exactly-once across process death* — is enforced by three layers, not magic:
1. **Provider-side idempotency.** Every destructive tool call carries an `Idempotency-Key`; the
   MCP Gateway / provider dedupes a replay of the same key (the primary guarantee, the same pattern
   Stripe uses).
2. **A race-safe durable ledger.** `AuditLog.claim_commit(key)` records the COMMIT atomically
   (file: an `flock`-guarded append; DynamoDB: a conditional `PutItem` on `COMMIT#{key}`), so two
   workers can't both record success and the check-then-commit window is closed.
3. **Live reconciliation on resume.** A fresh process rehydrates from the durable state + audit log
   and, for a `PENDING`-not-`COMMITTED` action, **queries the live system of record**
   (`RealWorld.is_reverted` → a read-only MCP tool) before re-acting — so it never re-fires a
   destructive action that already happened.

> The `scripts/run_demo.py` SSE stream animates intermediate beats with scripted pacing for the
> split-screen UI; the headline **Double-executions NAIVE:1 / DEADMAN:0** is computed from a real
> run of both agents, and `scripts/prove_exactly_once.py` asserts the invariant.

---

## 🗓️ 7-Day Plan (June 1–7)
| Day | Goal |
|---|---|
| Jun 1 | **De-risk the crown jewel** (this repo): external state machine + kill/resume → exactly-once. Start the X build-in-public thread. |
| Jun 2 | Exactly-once via the MCP audit log. **Go/no-go: must hold today.** |
| Jun 3 | AI Gateway depth: 5-tier fallback + latency-shed + semantic-cache runbook brain; verify trace tags. Post the kill-resume clip. |
| Jun 4 | MCP scoping + guardrails (Cedar deny, Pre/Post hooks). |
| Jun 5 | **Agent Gateway coupling (the WOW):** fallback-depth → auto-revoke authority. Post the auto-leash clip. |
| Jun 6 | Chaos UI + split-screen scoreboard. |
| Jun 7 | Rehearse ×5, record a deterministic take, final scoreboard tweet (tag @truefoundry), submit. |

## ⚠️ Biggest risk → how to kill it
**Risk:** it reads as "just a polished DevOps agent" and the meta-thesis doesn't land.
- Front-load the thesis **visually** in the first 30 sec (the Correlated Blackout button + "the firefighter is in the fire").
- Make **"Double-executions: 1 vs 0"** the one number on screen at the climax.
- Reserve the **auto-leash** flip as the second WOW — it proves the TrueFoundry-native thesis no other team can claim.
- **Secondary:** the Agent Gateway is brand-new (May 27) — treat the auto-leash coupling as a clean policy hook with a hand-rolled shim against the documented interface; never let an unfinished newest-product call block the deterministic core.

## 🧰 Tech Stack
AWS Bedrock (Claude/Llama/Mistral/Cohere) · TrueFoundry AI Gateway · TrueFoundry MCP Gateway + Guardrails · TrueFoundry Agent Gateway · Python.

## 📄 License
MIT — see [LICENSE](./LICENSE).
