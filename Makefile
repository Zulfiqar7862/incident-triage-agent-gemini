# Incident Triage Agent - everything you need from the terminal.
PY      ?= python3
VENV    := .venv
BIN     := $(VENV)/bin
PORT    ?= 8000

.PHONY: help install env run quickstart test lint eval demo clean

help:
	@echo "make install     - create .venv and install dependencies"
	@echo "make quickstart  - create .env (if missing) and start the app on http://localhost:$(PORT)"
	@echo "make run         - start the app (uses existing .env)"
	@echo "make test        - run the pytest suite (offline, no API key needed)"
	@echo "make lint        - ruff lint (same check as CI)"
	@echo "make eval        - run the golden-scenario eval harness"
	@echo "make demo        - drive a full run via curl against a running server"
	@echo "make clean       - delete the local database and caches"

install:
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required. Try: make install PY=python3.12")'
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip -q
	$(BIN)/pip install -e ".[dev]"
	@echo ""
	@echo "Installed. Next: make quickstart"

env:
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env from .env.example (add GEMINI_API_KEY to use Gemini; empty = offline mock mode)"; fi

run:
	$(BIN)/uvicorn incident_triage_agent.api.main:app --host 127.0.0.1 --port $(PORT)

quickstart: env
	@test -x $(BIN)/uvicorn || (echo "Run 'make install' first" && exit 1)
	@echo ""
	@echo "=============================================================="
	@echo "  UI:       http://localhost:$(PORT)"
	@echo "  API docs: http://localhost:$(PORT)/docs"
	@echo "  LLM mode: $$(grep -q '^GEMINI_API_KEY=..' .env && echo 'Gemini (key found in .env)' || echo 'offline mock (no key in .env)')"
	@echo "  Stop with Ctrl+C. Runs are saved in data/agent.db and resume after restart."
	@echo "=============================================================="
	$(BIN)/uvicorn incident_triage_agent.api.main:app --host 127.0.0.1 --port $(PORT)

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .

eval:
	$(BIN)/python -m evals.run_evals

demo:
	bash scripts/demo.sh

clean:
	rm -rf data .pytest_cache .ruff_cache evals/results src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
