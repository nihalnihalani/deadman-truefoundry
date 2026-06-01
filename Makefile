.PHONY: demo prove web serve test docker-build docker-up ui

# ── Demo / proof targets ────────────────────────────────────────────────────

demo:   ## Split-screen chaos demo: naive vs DEADMAN (stdlib, no setup)
	python3 scripts/run_demo.py

prove:  ## Day-1 crown jewel: kill mid-rollback, resume, assert exactly-once
	python3 scripts/prove_exactly_once.py

# ── Server targets ──────────────────────────────────────────────────────────

web:    ## Run the webhook (no --reload, matches production CMD)
	uvicorn deadman.webhook:app --host 0.0.0.0 --port 8080

serve:  ## Run with live reload (dev convenience)
	uvicorn deadman.webhook:app --reload --host 0.0.0.0 --port 8080

ui:     ## Open the split-screen web UI in the browser (starts server if needed)
	@echo "Starting server with live reload — open http://localhost:8080"
	uvicorn deadman.webhook:app --reload --host 0.0.0.0 --port 8080

# ── Test ────────────────────────────────────────────────────────────────────

test:   ## Run pytest suite
	python3 -m pytest -v

# ── Docker targets ──────────────────────────────────────────────────────────

docker-build:  ## Build the DEADMAN Docker image
	docker build -t deadman:local .

docker-up:     ## Run the app container (+ optional local-db / otel profiles)
	docker compose up deadman
