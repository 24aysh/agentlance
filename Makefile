.PHONY: check check-l2 check-l3 demo-l2 demo-l3 setup setup-l3 test format

check:
	uv run --locked python scripts/check_layer1.py

setup:
	uv sync --locked
	pnpm install --frozen-lockfile

test:
	uv run --locked pytest

format:
	uv run --locked ruff format .

check-l2:
	uv run --locked python scripts/check_layer2.py

demo-l2:
	mkdir -p .scratch
	uv run --locked python scripts/demo_layer2.py --work-dir $$(mktemp -d .scratch/layer2-demo.XXXXXX)

setup-l3:
	uv run --locked python scripts/layer3_tools.py --install

check-l3:
	uv run --locked python scripts/check_layer3.py

demo-l3:
	uv run --locked python scripts/demo_layer3.py
