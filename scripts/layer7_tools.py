"""Install checksummed upstream test contracts; build the reviewed evaluator only."""

import argparse
import json
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path

from layer3_tools import ROOT, TOOLCHAIN, archivePath, verifyArchive

DEPENDENCIES = {
    "jsonschema",
    "attrs",
    "jsonschema-specifications",
    "referencing",
    "rpds-py",
    "typing-extensions",
    "eth-hash",
    "pycryptodome",
}
SOURCES = [
    "modules/domain/records.py",
    "modules/validation/evaluator.py",
    "apps/validator/worker.py",
    "specs/schemas/protocol.schema.json",
]


def setupLayer7(*, install=False):
    lock = json.loads((ROOT / "contracts/layer7.lock.json").read_bytes())
    for name, spec in lock["libraries"].items():
        archive = archivePath(name + ".tar.gz", spec, install=install)
        destination = TOOLCHAIN / "lib" / name
        if install:
            with tempfile.TemporaryDirectory(dir=TOOLCHAIN) as temporary:
                with tarfile.open(archive) as source:
                    source.extractall(temporary, filter="data")
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(next(Path(temporary).iterdir())), destination)
        verifyArchive(archive, destination)
    if install:
        subprocess.run(["docker", "pull", lock["kuboImage"]], check=True)
    subprocess.run(
        ["docker", "image", "inspect", lock["kuboImage"]], check=True, stdout=subprocess.DEVNULL
    )
    return lock


def buildImage():
    context = ROOT / ".scratch/layer7/image"
    context.mkdir(parents=True, exist_ok=True)
    # Recreate a context containing only reviewed files, never the workspace or secrets.
    for path in context.iterdir():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    for name in SOURCES:
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    lines = []
    for package in packages:
        if package["name"] in DEPENDENCIES:
            lines.append(
                package["name"]
                + "=="
                + package["version"]
                + " "
                + " ".join("--hash=" + wheel["hash"] for wheel in package["wheels"])
            )
    if len(lines) != len(DEPENDENCIES):
        raise ValueError("Missing locked evaluator dependency")
    (context / "requirements.txt").write_text("\n".join(lines) + "\n")
    shutil.copyfile(ROOT / "apps/validator/Dockerfile", context / "Dockerfile")
    subprocess.run(["docker", "build", "-t", "agentlance-validation:l7", str(context)], check=True)
    image = subprocess.check_output(
        ["docker", "image", "inspect", "agentlance-validation:l7", "--format", "{{.Id}}"], text=True
    ).strip()
    (context.parent / "image-id").write_text(image + "\n")
    return image


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    setupLayer7(install=args.install)
    print(buildImage())
