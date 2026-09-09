.PHONY: check fix test lint types venv check-venv

VENV_PY := .venv/bin/python
BIN := $(if $(wildcard $(VENV_PY)),.venv/bin/,)

check-venv:
	@test -x "$(VENV_PY)" || { echo "no .venv: run \`python -m venv .venv && .venv/bin/pip install -e '.[dev]'\`" >&2; exit 1; }

check: check-venv lint types
	$(BIN)pytest -q

fix: check-venv
	$(BIN)ruff format src tests
	$(BIN)ruff check --fix src tests

test: check-venv
	$(BIN)pytest -q

lint: check-venv
	$(BIN)ruff check src tests
	$(BIN)ruff format --check src tests

types: check-venv
	$(BIN)pyright

venv:
	python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
