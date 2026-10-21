"""Build release boundaries without invoking Docker in offline tests."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[3] / "deploy/toolchain"
spec = importlib.util.spec_from_file_location("toolchain_builder", ASSETS / "build.py")
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_accepted_image_matches_locked_build_inputs():
    from yipilot.domain.remote_execution import TOOLS_IMAGE
    receipt = json.loads((ASSETS / "image.json").read_text())
    assert receipt["image_id"] == TOOLS_IMAGE
    for path, key in [(ASSETS / "Dockerfile", "dockerfile_sha256"),
                      (ASSETS.parents[1] / "uv.lock", "uv_lock_sha256")]:
        assert hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == receipt[key]


def test_accepted_receipt_cannot_be_overwritten(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b"accepted")
    with pytest.raises(ValueError, match="never overwrite"):
        builder.build(tmp_path, receipt, "uv")
    assert receipt.read_bytes() == b"accepted"


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_build_binds_locked_input_to_exact_image(tmp_path, monkeypatch, architecture):
    root = tmp_path / "source"
    (root / "deploy/toolchain").mkdir(parents=True)
    (root / "deploy/toolchain/Dockerfile").write_bytes((ASSETS / "Dockerfile").read_bytes())
    (root / "uv.lock").write_bytes(b"locked\n")
    image = "sha256:" + "a" * 64
    calls = []

    def command(*argv):
        calls.append(argv)
        if argv[0] == "uv":
            assert "--frozen" in argv and "--no-emit-project" in argv
            Path(argv[argv.index("--output-file") + 1]).write_bytes(b"pinned --hash=sha256:123\n")
            return ""
        assert argv[:3] == ("docker", "image", "inspect")
        return json.dumps([{"Id": image, "Os": "linux", "Architecture": architecture,
                            "Size": 123, "Config": {"Labels": {"org.yipilot.toolchain": "python-tools-v1"}}}])

    def run(argv, **kwargs):
        assert argv[:3] == ["docker", "build", "--pull=false"]
        assert kwargs["check"] and kwargs["timeout"] == 900
        Path(argv[argv.index("--iidfile") + 1]).write_text(image)

    monkeypatch.setattr(builder, "command", command)
    monkeypatch.setattr(builder.subprocess, "run", run)
    receipt = tmp_path / "receipt.json"
    if architecture == "arm64":
        with pytest.raises(ValueError, match="platform"):
            builder.build(root, receipt, "uv")
        assert not receipt.exists()
    else:
        result = builder.build(root, receipt, "uv")
        assert json.loads(receipt.read_text()) == result
        assert result["image_id"] == image and len(result["uv_lock_sha256"]) == 64
