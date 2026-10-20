"""Guard patch drift and exercise the real pinned wheel in Linux CI."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[3] / "deploy/opensandbox"
spec = importlib.util.spec_from_file_location("profile_builder", ASSETS / "build.py")
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_wrong_source_is_rejected_before_writing_a_release(tmp_path):
    source = tmp_path / "source"
    for name in builder.ORIGINALS:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not the pinned source")
    destination = tmp_path / "release/opensandbox_server"
    with pytest.raises(ValueError, match="source mismatch"):
        builder.build(source, destination)
    assert not destination.exists()


def test_existing_release_cannot_be_replaced(tmp_path):
    destination = tmp_path / "opensandbox_server"
    destination.mkdir()
    sentinel = destination / "sentinel"
    sentinel.write_bytes(b"original")
    with pytest.raises(ValueError, match="already exists"):
        builder.build(tmp_path / "unused", destination)
    assert sentinel.read_bytes() == b"original"


@pytest.mark.skipif(importlib.util.find_spec("opensandbox_server") is None or not shutil.which("patch"),
                    reason="Linux profile CI installs the pinned server wheel and patch utility")
def test_patch_builds_real_pinned_upstream_without_mutating_it(tmp_path):
    upstream = importlib.util.find_spec("opensandbox_server")
    source = Path(next(iter(upstream.submodule_search_locations)))
    before = {name: (source / name).read_bytes() for name in builder.ORIGINALS}
    destination = tmp_path / "release/opensandbox_server"
    receipt = builder.build(source, destination)
    assert receipt["upstream_server"] == "1.1.1"
    assert len(receipt["patched_sha256"]) == 7
    assert (destination / "yipilot_profile.py").read_bytes() == (ASSETS / "policy.py").read_bytes()
    assert (destination / "yipilot_control.py").read_bytes() == (ASSETS / "control.py").read_bytes()
    assert before == {name: (source / name).read_bytes() for name in builder.ORIGINALS}
