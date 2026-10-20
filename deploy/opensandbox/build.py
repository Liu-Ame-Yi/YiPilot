"""Build a separate patched package; never edit the installed upstream package."""

import argparse
import compileall
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ORIGINALS = {
    "api/proxy.py": "09e9ffaa83ec8894322a044a033d0e3d6fa683164b45cc53fd8245e44dc9ca93",
    "services/docker/container_ops.py": "fb852b4f88142e814ae4ef3f45503553e8f3f468bfab839cfff6f8cb165f1b13",
    "services/docker/docker_service.py": "b688b393a1ca24b63b6ffbcaf125cafb4445ef720d10f5264da08db0d3a0a451",
    "services/docker/networking.py": "ced0f2189c1025111dd7be87e504e85cd467b0638b73188c425814d8e5ade415",
    "services/docker/runtime.py": "84222fb1e69257ec433469efa0c5da8eaad220e9ed971e907677cbff2ae499be",
}


def build(source, destination):
    assets = Path(__file__).resolve().parent
    if destination.exists():
        raise ValueError("Release destination already exists; use a new empty release")
    for name, digest in ORIGINALS.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Upstream 1.1.1 source mismatch: {name}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    command = ["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(assets / "server.patch")]
    subprocess.run(command + ["--dry-run"], cwd=destination, check=True)
    subprocess.run(command, cwd=destination, check=True)
    shutil.copyfile(assets / "policy.py", destination / "yipilot_profile.py")
    shutil.copyfile(assets / "control.py", destination / "yipilot_control.py")
    if not compileall.compile_dir(destination, quiet=1, force=True):
        raise ValueError("Patched package does not compile")
    receipt = {
        "upstream_server": "1.1.1",
        "source_sha256": ORIGINALS,
        "assets_sha256": {name: hashlib.sha256((assets / name).read_bytes()).hexdigest()
                          for name in ("server.patch", "policy.py", "control.py", "build.py")},
        "patched_sha256": {name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
                           for name in (*ORIGINALS, "yipilot_profile.py", "yipilot_control.py")},
    }
    (destination.parent / "profile-build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.destination), indent=2))
