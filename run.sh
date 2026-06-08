#!/usr/bin/env bash
#
# run.sh — fully run the DEADMAN app end to end, with debug logging.
#
#   ./run.sh
#
# What it does, in order:
#   1. Loads .env (so DEADMAN_MODE and the TFY/AWS credentials take effect).
#   2. Creates .venv and installs requirements.txt if they are missing.
#   3. In real mode, if TFY_MCP_GATEWAY_URL points at localhost, starts the
#      local safe MCP server and waits for it to listen.
#   4. In real mode, runs the safe wiring check (scripts/real_doctor.py).
#      This makes ONE small, billed model call. Skip it with DEADMAN_RUN_PREFLIGHT=0.
#   5. Starts the DEADMAN webhook (uvicorn) with DEBUG-level logging and streams
#      the logs to the console. Ctrl-C cleanly tears everything down.
#
# Everything is also written to ./logs/ (run.log, app.log, mcp.log, doctor.log,
# pip.log) for debugging.
#
# Useful overrides (env vars):
#   DEADMAN_PORT=8080            webhook port
#   DEADMAN_MCP_PORT=8000        local MCP server port
#   DEADMAN_HOST=127.0.0.1       bind host
#   DEADMAN_RUN_PREFLIGHT=0      skip the billed real_doctor preflight
#   RUN_SH_HEALTHCHECK_ONLY=1    boot, health-check, then exit (smoke test)
#   DEADMAN_TRACE=1              enable bash xtrace into logs/trace.log
#   PYTHON_BIN=python3.12        interpreter used to create the venv
#
set -euo pipefail

# ── Locate the repo root (works regardless of where it's called from) ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Log directory + helpers ───────────────────────────────────────────────────
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run.log"
APP_LOG="$LOG_DIR/app.log"
MCP_LOG="$LOG_DIR/mcp.log"
PIP_LOG="$LOG_DIR/pip.log"
DOCTOR_LOG="$LOG_DIR/doctor.log"

# Fresh app/mcp logs each run so streamed output isn't a replay of an old boot.
: > "$APP_LOG"
: > "$MCP_LOG"

_ts() { date +%H:%M:%S; }
log()   { printf '\033[36m[run %s]\033[0m %s\n'  "$(_ts)" "$*" | tee -a "$RUN_LOG"; }
debug() { printf '\033[90m[dbg %s]\033[0m %s\n'  "$(_ts)" "$*" | tee -a "$RUN_LOG"; }
warn()  { printf '\033[33m[warn %s]\033[0m %s\n' "$(_ts)" "$*" | tee -a "$RUN_LOG" >&2; }
err()   { printf '\033[31m[ERR %s]\033[0m %s\n'  "$(_ts)" "$*" | tee -a "$RUN_LOG" >&2; }

export PYTHONUNBUFFERED=1

# Optional bash-level trace for deep debugging.
if [ "${DEADMAN_TRACE:-0}" = "1" ]; then
  exec 9>"$LOG_DIR/trace.log"
  export BASH_XTRACEFD=9
  set -x
  debug "bash xtrace enabled -> $LOG_DIR/trace.log"
fi

# ── Background-process bookkeeping + cleanup ──────────────────────────────────
PIDS=()
cleanup() {
  local code=$?
  debug "cleanup: stopping background processes (${PIDS[*]:-none})"
  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
  done
  # Give children a moment, then hard-kill any stragglers.
  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid:-}" ] && kill -9 "$pid" 2>/dev/null || true
  done
  log "shutdown complete"
  exit "$code"
}
trap cleanup EXIT INT TERM

# Wait until a TCP host:port accepts a connection (pure bash, no nc needed).
wait_port() {
  local host="$1" port="$2" tries="${3:-75}" i
  for ((i = 1; i <= tries; i++)); do
    if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
      exec 3>&- 3<&- 2>/dev/null || true
      return 0
    fi
    sleep 0.2
  done
  return 1
}

log "DEADMAN run.sh starting in $SCRIPT_DIR"

# ── 1. Load .env ──────────────────────────────────────────────────────────────
if [ -f .env ]; then
  log "loading .env (variables already set in the environment take precedence)"
  while IFS= read -r _line || [ -n "$_line" ]; do
    # trim leading whitespace, skip blanks + comments
    _line="${_line#"${_line%%[![:space:]]*}"}"
    case "$_line" in ''|'#'*) continue ;; esac
    case "$_line" in export\ *) _line="${_line#export }" ;; esac
    _key="${_line%%=*}"
    _val="${_line#*=}"
    [ "$_key" = "$_line" ] && continue   # no '=' on the line
    # strip one layer of matching surrounding quotes
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
    if [ -z "${!_key:-}" ]; then
      export "$_key=$_val"
    fi
  done < .env
else
  warn ".env not found — defaulting to mock mode (copy .env.example to .env for real mode)"
fi

MODE="${DEADMAN_MODE:-mock}"
HOST="${DEADMAN_HOST:-127.0.0.1}"
PORT="${DEADMAN_PORT:-8080}"
MCP_PORT="${DEADMAN_MCP_PORT:-8000}"
debug "mode=$MODE host=$HOST port=$PORT mcp_port=$MCP_PORT"

# In mock mode the demo/chaos endpoints + UI are useful; enable unless told not to.
if [ "$MODE" = "mock" ] && [ -z "${DEADMAN_ENABLE_DEMO:-}" ]; then
  export DEADMAN_ENABLE_DEMO=1
  debug "mock mode: enabling demo/chaos endpoints (DEADMAN_ENABLE_DEMO=1)"
fi

# ── 2. venv + dependencies ────────────────────────────────────────────────────
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="$SCRIPT_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  log "creating virtualenv at .venv (using $PYTHON_BIN)"
  "$PYTHON_BIN" -m venv "$VENV"
fi
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

if ! "$PY" -c 'import fastapi, uvicorn' 2>/dev/null; then
  log "installing requirements.txt (first run can take a minute) -> $PIP_LOG"
  "$PIP" install --upgrade pip   >"$PIP_LOG" 2>&1
  "$PIP" install -r requirements.txt >>"$PIP_LOG" 2>&1
  log "dependencies installed"
else
  debug "dependencies already present"
fi

# ── 3. Configuration readiness report ─────────────────────────────────────────
log "configuration readiness:"
"$PY" -c "import deadman.config as c, json; print(json.dumps(c.readiness(), indent=2))" \
  | tee -a "$RUN_LOG"

# ── 4. Local MCP server (real mode + localhost target only) ───────────────────
needs_local_mcp() {
  [ "$MODE" = "real" ] || return 1
  case "${TFY_MCP_GATEWAY_URL:-}" in
    *127.0.0.1*|*localhost*) return 0 ;;
    *) return 1 ;;
  esac
}

if needs_local_mcp; then
  log "real mode + local MCP target -> starting safe MCP server on $HOST:$MCP_PORT -> $MCP_LOG"
  "$PY" mcp_servers/deadman_safe_tools.py --transport http --host "$HOST" --port "$MCP_PORT" \
    >>"$MCP_LOG" 2>&1 &
  MCP_PID=$!
  PIDS+=("$MCP_PID")
  if wait_port "$HOST" "$MCP_PORT"; then
    log "MCP server is listening (pid $MCP_PID)"
  else
    err "MCP server did not start — see $MCP_LOG"
    tail -n 20 "$MCP_LOG" >&2 || true
    exit 1
  fi
else
  debug "no local MCP server needed (mode=$MODE, url=${TFY_MCP_GATEWAY_URL:-unset})"
fi

# ── 5. Real-mode preflight wiring check ───────────────────────────────────────
if [ "$MODE" = "real" ] && [ "${DEADMAN_RUN_PREFLIGHT:-1}" = "1" ]; then
  log "real-mode preflight: scripts/real_doctor.py (makes ONE small billed model call) -> $DOCTOR_LOG"
  if "$PY" scripts/real_doctor.py 2>&1 | tee "$DOCTOR_LOG"; then
    log "preflight passed"
  else
    warn "preflight reported issues (continuing to start the server anyway — see $DOCTOR_LOG)"
  fi
elif [ "$MODE" = "real" ]; then
  debug "preflight skipped (DEADMAN_RUN_PREFLIGHT=0)"
fi

# ── 6. Start the webhook app with DEBUG logging ───────────────────────────────
log "starting DEADMAN webhook on http://$HOST:$PORT (mode=$MODE, debug logging on) -> $APP_LOG"
"$PY" - "$HOST" "$PORT" >>"$APP_LOG" 2>&1 <<'PYEOF' &
import sys, logging
from deadman.logging_config import configure_logging
configure_logging()
# Crank everything up to DEBUG for full visibility.
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger("deadman").setLevel(logging.DEBUG)
import uvicorn
host, port = sys.argv[1], int(sys.argv[2])
uvicorn.run("deadman.webhook:app", host=host, port=port, log_level="debug")
PYEOF
APP_PID=$!
PIDS+=("$APP_PID")

# Stream the app's debug log to the console while it runs.
tail -n +1 -f "$APP_LOG" &
TAIL_PID=$!
PIDS+=("$TAIL_PID")

# ── Wait for liveness, then report readiness ──────────────────────────────────
if wait_port "$HOST" "$PORT"; then
  # Give uvicorn a beat to register routes.
  sleep 0.5
  log "webhook is up — GET /readyz:"
  "$PY" - "$HOST" "$PORT" <<'PYEOF' | tee -a "$RUN_LOG" || true
import sys, json, urllib.request
host, port = sys.argv[1], int(sys.argv[2])
try:
    with urllib.request.urlopen(f"http://{host}:{port}/readyz", timeout=5) as r:
        print(json.dumps(json.loads(r.read()), indent=2))
except urllib.error.HTTPError as e:
    print(f"/readyz -> HTTP {e.code}: {e.read().decode()}")
except Exception as e:
    print(f"/readyz check failed: {e}")
PYEOF
  log "endpoints:  http://$HOST:$PORT/   (UI)   /healthz   /readyz   /metrics   POST /incident"
else
  err "webhook did not start listening on $HOST:$PORT — see $APP_LOG"
  tail -n 30 "$APP_LOG" >&2 || true
  exit 1
fi

# Smoke-test mode: prove it boots, then exit (used for verification / CI).
if [ "${RUN_SH_HEALTHCHECK_ONLY:-0}" = "1" ]; then
  log "RUN_SH_HEALTHCHECK_ONLY=1 -> boot verified, exiting"
  exit 0
fi

log "DEADMAN is running. Press Ctrl-C to stop."
# Block on the server; when it exits (or Ctrl-C fires the trap) we clean up.
wait "$APP_PID"
