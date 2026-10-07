"""Repository-local, checksummed L3 toolchain setup and verification."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN = ROOT / ".scratch/layer3/toolchain"
DOWNLOADS = ROOT / ".scratch/layer3/downloads"


def loadToolchain():
    return json.loads((ROOT / "contracts/toolchain.lock.json").read_text())


def platformNames():
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin" and machine in {"arm64", "aarch64", "x86_64"}:
        return "darwin_arm64" if machine in {"arm64", "aarch64"} else "darwin_amd64", "macosx-amd64"
    if system == "Linux" and machine == "x86_64":
        return "linux_amd64", "linux-amd64"
    raise RuntimeError(f"No qualified pinned toolchain for {system}/{machine}")


def toolPath(name):
    if name not in {"forge", "anvil", "cast", "solc"}:
        raise ValueError(f"Unknown L3 tool: {name}")
    path = TOOLCHAIN / "bin" / name
    if not path.is_file():
        raise RuntimeError(f"Missing pinned L3 {name}; run make setup-l3")
    return str(path)


def fileDigest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archivePath(name, spec, *, install=False):
    path = DOWNLOADS / name
    if not path.is_file():
        if not install:
            raise RuntimeError(f"Missing toolchain archive {name}; run make setup-l3")
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        with (
            urllib.request.urlopen(spec["url"], timeout=60) as source,
            temporary.open("wb") as target,
        ):
            shutil.copyfileobj(source, target)
        temporary.replace(path)
    if fileDigest(path) != spec["sha256"]:
        raise RuntimeError(f"Toolchain checksum mismatch: {path}")
    return path


def verifyArchive(path, destination, names=None):
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = Path(member.name)
            relative = relative if names else Path(*relative.parts[1:])
            if names and relative.name not in names:
                continue
            installed = destination / relative
            source = archive.extractfile(member)
            if source is None or not installed.is_file():
                raise RuntimeError(f"Missing installed toolchain file: {installed}")
            if fileDigest(installed) != hashlib.sha256(source.read()).hexdigest():
                raise RuntimeError(f"Modified pinned toolchain file: {installed}")


def setupToolchain():
    lock = loadToolchain()
    foundryPlatform, solcPlatform = platformNames()
    binaries = TOOLCHAIN / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    foundry = archivePath(
        "foundry-" + foundryPlatform + ".tar.gz",
        lock["foundry"]["archives"][foundryPlatform],
        install=True,
    )
    with tarfile.open(foundry) as archive:
        for name in ("forge", "anvil", "cast"):
            member = next(
                m for m in archive.getmembers() if Path(m.name).name == name and m.isfile()
            )
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"Missing release binary {name}")
            (binaries / name).write_bytes(source.read())
            (binaries / name).chmod(0o755)
    solc = archivePath("solc-" + solcPlatform, lock["solc"]["binaries"][solcPlatform], install=True)
    shutil.copyfile(solc, binaries / "solc")
    (binaries / "solc").chmod(0o755)
    for name, spec in lock["libraries"].items():
        archiveFile = archivePath(name + ".tar.gz", spec, install=True)
        destination = TOOLCHAIN / "lib" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TOOLCHAIN) as temporary:
            with tarfile.open(archiveFile) as archive:
                archive.extractall(temporary, filter="data")
            source = next(Path(temporary).iterdir())
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(source), destination)
    return verifyToolchain()


def verifyToolchain():
    lock = loadToolchain()
    foundryPlatform, solcPlatform = platformNames()
    archive = archivePath(
        "foundry-" + foundryPlatform + ".tar.gz", lock["foundry"]["archives"][foundryPlatform]
    )
    verifyArchive(archive, TOOLCHAIN / "bin", {"forge", "anvil", "cast"})
    if fileDigest(Path(toolPath("solc"))) != lock["solc"]["binaries"][solcPlatform]["sha256"]:
        raise RuntimeError("Modified pinned solc binary")
    for name, spec in lock["libraries"].items():
        verifyArchive(archivePath(name + ".tar.gz", spec), TOOLCHAIN / "lib" / name)
    versions = {}
    for name in ("forge", "anvil", "cast", "solc"):
        version = subprocess.run(
            [toolPath(name), "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        expected = lock["solc"]["version"] if name == "solc" else lock["foundry"]["revision"]
        if expected not in version:
            raise RuntimeError(f"Unexpected {name} version: {version}")
        versions[name] = version
    return {"versions": versions, "execution": lock["execution"], "dependencies": lock["libraries"]}


def verifyExecutionProfile():
    result = subprocess.run(
        [toolPath("forge"), "config", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    expected = {
        "network": "monad",
        "hardfork": "monad:MonadTen",
        "evm_version": "paris",
        "optimizer": True,
        "optimizer_runs": 200,
        "via_ir": True,
        "solc": ".scratch/layer3/toolchain/bin/solc",
        "code_size_limit": None,
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise RuntimeError(f"Unqualified execution/compiler setting {key}: {config.get(key)}")
    requiredRemappings = {
        "@openzeppelin/contracts/=.scratch/layer3/toolchain/lib/openzeppelin-contracts/contracts/",
        "forge-std/=.scratch/layer3/toolchain/lib/forge-std/src/",
    }
    if not requiredRemappings <= set(config["remappings"]):
        raise RuntimeError("Pinned source-library remappings changed")
    campaigns = {
        "fuzzRuns": config["fuzz"]["runs"],
        "fuzzSeed": config["fuzz"]["seed"],
        "invariantRuns": config["invariant"]["runs"],
        "invariantDepth": config["invariant"]["depth"],
    }
    if campaigns != {
        "fuzzRuns": 256,
        "fuzzSeed": "0x6c6179657233",
        "invariantRuns": 64,
        "invariantDepth": 128,
    }:
        raise RuntimeError(f"Unqualified fuzz/invariant campaign settings: {campaigns}")
    return expected | campaigns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    result = setupToolchain() if args.install else verifyToolchain()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
