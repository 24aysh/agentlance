.PHONY: check setup test format

check:
	uv run --locked python scripts/check_layer1.py

setup:
	uv sync --locked
	pnpm install --frozen-lockfile

test:
	uv run --locked pytest

format:
	uv run --locked ruff format .
