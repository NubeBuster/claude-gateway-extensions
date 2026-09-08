.PHONY: check fix test lint types

BIN := $(if $(wildcard .venv/bin/python),.venv/bin/,)

check: lint types
	$(BIN)pytest -q

fix:
	$(BIN)ruff format src tests
	$(BIN)ruff check --fix src tests

test:
	$(BIN)pytest -q

lint:
	$(BIN)ruff check src tests
	$(BIN)ruff format --check src tests

types:
	$(BIN)pyright
