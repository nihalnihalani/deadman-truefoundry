.PHONY: demo prove web
demo:   ## Split-screen chaos demo: naive vs DEADMAN (stdlib, no setup)
	python scripts/run_demo.py
prove:  ## Day-1 crown jewel: kill mid-rollback, resume, assert exactly-once
	python scripts/prove_exactly_once.py
web:    ## Run the real-mode webhook locally
	uvicorn deadman.webhook:app --reload --port 8080
