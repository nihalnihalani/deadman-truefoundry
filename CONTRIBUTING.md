# Contributing to DEADMAN

Quick guide for running quality gates locally before pushing.

## Setup

```bash
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install          # install git hooks (one-time)
```

## Running tests

```bash
python3 -m pytest -q                         # fast (quiet)
python3 -m pytest -v                         # verbose
python3 -m pytest --cov=deadman --cov-report=term-missing  # with coverage
```

The test suite requires no external services — all heavy dependencies
(Bedrock, DynamoDB, TrueFoundry) are mocked.

## Lint + format

```bash
python3 -m ruff check .         # lint
python3 -m ruff check . --fix   # lint + auto-fix
python3 -m ruff format .        # format
```

Pre-commit runs both automatically on `git commit`.

## Type checking

```bash
python3 -m mypy deadman
```

The baseline is intentionally lenient (`ignore_missing_imports`, no
`disallow_untyped_defs`). The gate passes on the current codebase.
Per-module overrides in `pyproject.toml` suppress errors in files that
pre-date CI; remove them as those modules are cleaned up.

## Security

```bash
pip-audit -r requirements.txt                     # known CVEs in deps
python3 -m bandit -r deadman --severity-level high --confidence-level high
```

## Demo / proof

```bash
python3 scripts/run_demo.py           # split-screen chaos demo
python3 scripts/prove_exactly_once.py # Day-1 exactly-once proof
```

## CI

All of the above runs automatically in GitHub Actions on every PR and push to `main`.
See `.github/workflows/ci.yml` for the full pipeline.
