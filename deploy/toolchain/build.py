"""Build a cached tool image from uv.lock; publish its immutable local image ID."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def command(*argv):
    return subprocess.check_output(argv, timeout=900).decode().strip()


def build(root, receipt, uv):
    if receipt.exists():
        raise ValueError("Use a new build receipt; never overwrite an accepted release")
    dockerfile = root / "deploy/toolchain/Dockerfile"
    with TemporaryDirectory(prefix="yipilot-tools-") as temporary:
        context = Path(temporary)
        (context / "Dockerfile").write_bytes(dockerfile.read_bytes().replace(b"\r\n", b"\n"))
        requirements = context / "requirements.txt"
        command(uv, "export", "--project", str(root), "--frozen", "--group", "dev",
                "--no-emit-project", "--output-file", str(requirements), "--quiet")
        subprocess.run(["docker", "build", "--pull=false", "--iidfile", str(context / "image.id"), str(context)],
                       check=True, timeout=900)
        image = (context / "image.id").read_text().strip()
        facts = json.loads(command("docker", "image", "inspect", image))[0]
        if (facts["Id"] != image or facts["Os"] != "linux" or facts["Architecture"] != "amd64"
                or facts["Config"].get("Labels", {}).get("org.yipilot.toolchain") != "python-tools-v1"):
            raise ValueError("Built image does not match the required platform and toolchain")
        result = {
            "image_id": image, "platform": "linux/amd64", "size_bytes": facts["Size"],
            "dockerfile_sha256": hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest(),
            "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    print(json.dumps(build(args.root.resolve(strict=True), args.receipt, args.uv), indent=2))
