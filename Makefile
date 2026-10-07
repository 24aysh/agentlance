.PHONY: check check-l2 check-l3 check-l4 check-l5 demo-l2 demo-l3 demo-l4 demo-l5 setup setup-l3 test format

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

check-l4:
	uv run --locked python scripts/check_layer4.py

demo-l4:
	uv run --locked python scripts/demo_layer4.py

check-l5:
	uv run --locked python scripts/check_layer5.py

demo-l5:
	uv run --locked python scripts/demo_layer5.py
