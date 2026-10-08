"""Build the reviewed local worker image and record its immutable config ID."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def buildImage():
    scratch = ROOT / ".scratch/layer6"
    scratch.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "apps/reference_agent/Dockerfile",
            "-t",
            "agentlance-execution:l6",
            ".",
        ],
        cwd=ROOT,
        check=True,
    )
    image = subprocess.check_output(
        ["docker", "image", "inspect", "agentlance-execution:l6", "--format", "{{.Id}}"], text=True
    ).strip()
    (scratch / "image-id").write_text(image + "\n")
    return image


if __name__ == "__main__":
    print(buildImage())
