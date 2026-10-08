.PHONY: setup-l7 check-l7 demo-l7 check check-l2 check-l3 check-l4 check-l5 demo-l2 demo-l3 demo-l4 demo-l5 check-l6 demo-l6 setup-l6 setup setup-l3 test format

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

setup-l6:
	uv run --locked python scripts/layer6_image.py

check-l6:
	uv run --locked python scripts/check_layer6.py

demo-l6:
	uv run --locked python scripts/demo_layer6.py

setup-l7:
	uv run --locked python scripts/layer7_tools.py --install

check-l7:
	uv run --locked python scripts/check_layer7.py

demo-l7:
	uv run --locked python scripts/demo_layer7.py

.PHONY: check-l8 demo-l8
check-l8:
	uv run --locked python scripts/check_layer8.py

demo-l8:
	uv run --locked python scripts/demo_layer8.py
