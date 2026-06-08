# DEADMAN — 3-Minute Demo Run-of-Show

> The AI incident-commander agent that **survives its own outage.**
> TrueFoundry "Resilient Agents" hackathon · sponsored by AWS Bedrock.

This is a recording-ready script. Read it once, do one dry run, then shoot.

---

## One-number hook (open with this)

> **"Most agents double-execute when they crash mid-action. DEADMAN executed the
> rollback EXACTLY ONCE across a SIGKILL — zero double-executions — and it proves
> it live, not in a slide."**

Numbers you will show on camera: **NAIVE `double_executions=1` vs DEADMAN
`double_executions=0`**, plus **`drain_authority=OFF`** (the agent revoked its own
destructive power) and **`guardrail_blocks=1`**.

---

## Setup (once, before recording)

From the repo root (`deadman-truefoundry/`):

```bash
# 1. (Recommended) virtualenv
python3 -m venv .venv && source .venv/bin/activate

# 2. Install
python3 -m pip install -e .
# (if that fails, the deps live in pyproject.toml: fastapi, uvicorn, pydantic)

# 3. Start the war-room server — MOCK mode so the demo/chaos endpoints are live.
#    The demo + chaos buttons are AUTO-DISABLED in real mode unless you opt in,
#    so for the on-stage UI demo run in mock mode (or pass DEADMAN_ENABLE_DEMO=1).
DEADMAN_MODE=mock DEADMAN_ENABLE_DEMO=1 uvicorn deadman.webhook:app --host 0.0.0.0 --port 8080
```

Then open the UI: **http://localhost:8080**

> If port 8080 is taken, use `--port 8090` and open `http://localhost:8090`
> (and pass `--base-url http://localhost:8090` to `demo_drive.py` below).

**Env notes**
- `DEADMAN_MODE=mock` (default): deterministic, offline, no AWS creds needed. Use this for the screen-record.
- `DEADMAN_MODE=real`: genuine TrueFoundry AI Gateway + Bedrock + MCP Gateway. Needed only for the **live Bedrock failover** segment. Verify wiring with the direct-Bedrock path (works without TFY credit): `DEADMAN_LLM_BACKEND=bedrock python3 scripts/real_doctor.py --skip-mcp --skip-dynamodb`. The full-TFY check (`python3 scripts/real_doctor.py`) needs the TrueFoundry tenant to have credit.
- Two terminals on screen is ideal: **Terminal A** = server logs (structured JSON), **Terminal B** = the CLI proofs.

### Optional: auto-drive the UI (hands-free recording)

Instead of clicking buttons by hand, you can let a script drive the server while
you narrate. With the server running, in Terminal B:

```bash
python3 scripts/demo_drive.py --pause 3
```

It resets chaos, fires every chaos toggle in order, runs the NAIVE-vs-DEADMAN
comparison, and prints the `/metrics` surface — pausing 3s before each step so the
split-screen UI updates on camera. (Use `--base-url` if not on :8080.)

---

## The 3-minute script

> UI button labels below are the **exact** on-screen text (verified in `web/app.js`).
> Chaos toggles map 1:1 to `POST /api/chaos/{toggle}`.

| Time | Action (click / command) | On screen | Say (1–2 sentences) | Judging criterion |
|------|--------------------------|-----------|---------------------|-------------------|
| 0:00 | Show UI at `localhost:8080`. Click **↺ Reset**. | Split-screen: **NAIVE** (raw Bedrock) vs **DEADMAN** (TFY Gateway), both green. | "Two agents handle the same incident. Left is a naive agent on raw Bedrock; right is DEADMAN through the TrueFoundry AI Gateway." | UX still works / setup |
| 0:15 | Click **☠ Correlated Blackout**. | Both backends flip to "degraded"; timeline notes the us-east-1 event. | "We inject a correlated blackout — region, provider, and tools fail at once. This is the failure we introduced." | Failure introduced |
| 0:30 | Click **☠ All-Bedrock-Down**. | DEADMAN: `drain_authority` flips **ON → OFF**; note "AUTO-LEASH: destructive authority REVOKED". | "As its brain degrades to a weaker model, DEADMAN **revokes its own destructive authority** — auto-leash. A dumber agent shouldn't be allowed to delete prod." | Guardrail / handling |
| 0:50 | Click **⚡ 429 Storm**. | DEADMAN: `fallback_depth` rises, backend → tier-1 (us-west-2). | "A 429 storm hits tier-0; the AI Gateway sheds latency and fails over to a healthy tier. The user experience never stalls." | Fallback / retry |
| 1:05 | Click **⚠ Corrupt Output**. | DEADMAN: `guardrail_blocks=1`; note "Post-Tool guardrail caught corrupt JSON". | "A degraded tool returns garbage JSON. The Post-Tool guardrail catches it before it ever reaches an action." | Guardrail |
| 1:20 | Click **🔴 KILL mid-rollback**, then **▶ RUN CORRELATED BLACKOUT**. | SSE timeline streams beats; DEADMAN survives, NAIVE dies. Headline: NAIVE `double_executions=1`, DEADMAN `double_executions=0`. | "Now the hard one: SIGKILL lands between the rollback's side-effect and its commit. Watch what each agent does." | Recovery / exactly-once |
| 1:45 | Point at the scoreboard. | NAIVE: DEAD, `double_executions=1`. DEADMAN: SURVIVED, `double_executions=0`. | "The naive agent restarts from scratch and **double-fires the revert** — a duplicated prod action. DEADMAN resumes from durable state and skips the already-committed step." | Exactly-once / why UX works |
| 2:00 | **Terminal B:** `python3 scripts/prove_exactly_once.py` | Prints `[PASS] exactly-once across process death ✓`. | "That's not a UI animation. This script actually kills the process mid-rollback, resumes a fresh one, and asserts the revert ran exactly once." | Proof / credibility |
| 2:25 | **Terminal B:** `python3 scripts/bedrock_failover_demo.py --down 2` | Real cross-provider Bedrock failover output (served-by / tier changes). | "And it's real Bedrock, not staged — I take down two providers live and the AI Gateway fails over across them." | AWS Bedrock / TFY Gateway / not staged |
| 2:50 | Hold on the final scoreboard. | DEADMAN SURVIVED · 0 double-executions · authority OFF · guardrail caught. | (Credibility close below.) | Wrap |

> **If you skip the optional auto-drive:** click the buttons in the order above.
> **If you use it:** at 0:15 run `python3 scripts/demo_drive.py --pause 3` in
> Terminal B and narrate the rows as they print; jump to 2:00 when it finishes.

---

## Credibility close (say this at 2:50)

> **"DEADMAN is real, tested, and ships. 364 tests passing, CI green, real-mode
> wired to TrueFoundry's AI Gateway, MCP Gateway, and AWS Bedrock with guardrails.
> The naive agent corrupted production. DEADMAN didn't. We ship Monday."**

The four required surfaces are all on screen across the run:
- **AWS Bedrock** — the model backend (NAIVE raw vs DEADMAN gatewayed; live failover at 2:25).
- **TrueFoundry AI Gateway** — tiered failover + latency shedding (0:50).
- **MCP Gateway** — the tool layer the agent acts through (validate with `real_doctor.py`).
- **Guardrails** — Post-Tool corrupt-output block (1:05) + auto-leash authority revocation (0:30).

---

## Backup / if something fails live

| If this breaks… | Do this |
|-----------------|---------|
| UI won't load / "BACKEND OFFLINE" banner | The page is designed to stay usable offline. Restart the server (Setup step 3) and refresh; or fall back to the CLI: `python3 scripts/run_demo.py` (mock scoreboard, no server needed). |
| Chaos buttons return 404 | The server is in **real** mode (demo endpoints are hidden). Restart with `DEADMAN_MODE=mock DEADMAN_ENABLE_DEMO=1 …`. |
| Port 8080 in use | Start with `--port 8090`, open `localhost:8090`, and pass `--base-url http://localhost:8090` to `demo_drive.py`. |
| SSE timeline (▶ RUN) looks stuck | The UI auto-falls back to `POST /api/demo/run`; or hit it directly: `curl -s -X POST localhost:8080/api/demo/run | python3 -m json.tool`. |
| Live Bedrock segment (`bedrock_failover_demo.py`) is flaky / no creds | Skip it and lean on `scripts/prove_exactly_once.py` — it's deterministic and offline. Say: "the live failover is in the README; here's the deterministic proof." Then re-run if time allows. |
| `prove_exactly_once.py` errors | Re-run once (it resets its own state). As a last resort, show the UI headline (NAIVE=1, DEADMAN=0), which exercises the same durable-state path. |
| `real_doctor.py` shows FAIL | That's a wiring/creds issue, not a code issue — stay in mock mode for the demo and mention real mode is gated behind readiness checks. |

**Pre-flight checklist (run all green before recording):**
```bash
python3 scripts/run_demo.py          # mock scoreboard prints
python3 scripts/prove_exactly_once.py # prints [PASS] exactly-once ✓
# server up, then:
python3 scripts/demo_drive.py --pause 0.5   # drives all endpoints, prints headline
```

---

## Must-customize before publishing (human TODO)

- [ ] **Your name / team name** in the credibility close and video intro.
- [ ] **Repo / demo URL** to show on the closing card.
- [ ] **Test count** — script says **364**; re-run `python3 -m pytest -q` and update the number if it changed.
- [ ] **"ships Monday"** — adjust the date to your actual ship target.
- [ ] Confirm `scripts/bedrock_failover_demo.py` exists and the `--down 2` output matches the narration (built by a teammate; rehearse it once with real creds).
- [ ] Decide hands-on clicking vs `demo_drive.py` auto-drive and rehearse that path only.
