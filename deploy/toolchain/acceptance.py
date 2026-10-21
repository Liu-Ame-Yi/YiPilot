"""Trusted tool fixture and capacity measurements on the drained Linux Runner."""

import json
import os
import secrets
import subprocess
import time
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx

from yipilot.domain.remote_execution import TOOLS_IMAGE, TOOLS_PROFILE
from yipilot.runner.runtime import writer_group
from yipilot.workspace.remote_bundle import freeze_bundle
from yipilot.workspace.runner_client import RunnerClient

ROOT = Path("/srv/yipilot-runner")
SCRIPT = '''import importlib.metadata,json,pathlib,subprocess,sys,time
pathlib.Path('main.py').unlink()
pathlib.Path('started.txt').write_text(str(time.time()))
commands=[['git','-c','core.hooksPath=/dev/null','apply','--no-index','--whitespace=nowarn','--','approved.diff'],
          ['python','-I','-B','-m','pytest','-p','no:cacheprovider'],
          ['ruff','check','src','tests'],['mypy','src']]
results=[]
for argv in commands:
    result=subprocess.run(argv,capture_output=True,text=True,timeout=60)
    results.append({'tool':argv[0] if argv[0]!='python' else 'pytest','exit_code':result.returncode,
                    'stdout':result.stdout,'stderr':result.stderr})
    assert result.returncode==0,results
pathlib.Path('approved.diff').unlink()
time.sleep(4)
pathlib.Path('finished.txt').write_text(str(time.time()))
print(json.dumps({'checks':results,'python':sys.version.split()[0],
    'versions':{name:importlib.metadata.version(name) for name in ['pytest','ruff','mypy']}}))
'''
LIGHT = '''import pathlib,time
pathlib.Path('main.py').unlink()
payload=bytearray(48*1024**2)
pathlib.Path('started.txt').write_text(str(time.time()))
time.sleep(8)
pathlib.Path('finished.txt').write_text(str(time.time()))
'''


def run():
    assert os.name == "posix" and os.geteuid() == 0
    assert not subprocess.check_output(["docker", "ps", "-aq"]).strip()
    configuration = tomllib.loads(Path("/etc/yipilot-runner.toml").read_text())
    report = {"trusted_fixtures_only": True, "profile": TOOLS_PROFILE, "image": TOOLS_IMAGE,
              "business_cutover": False, "jobs": [], "capacity": []}
    probes = ROOT / "probes"
    probes.mkdir(mode=0o700, exist_ok=True)
    with TemporaryDirectory(dir=probes, prefix="tools-") as temporary, httpx.Client(
            base_url="http://127.0.0.1:8090", trust_env=False, timeout=40,
            headers={"Authorization": "Bearer " + configuration["runner_key"]}) as http:
        client, base = RunnerClient(http), Path(temporary)

        def fixture(code, memory, cpu, *, tools=False):
            key = "tools-" + secrets.token_hex(6)
            source = base / key
            source.mkdir()
            (source / "main.py").write_text(code)
            if tools:
                (source / "src").mkdir()
                (source / "tests").mkdir()
                (source / "src/sample.py").write_text("def answer() -> int:\n    return 42\n")
                (source / "tests/test_sample.py").write_text("from sample import answer\n\n\ndef test_answer():\n    assert answer() == 43\n")
                (source / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
                (source / "approved.diff").write_text("diff --git a/src/sample.py b/src/sample.py\n--- a/src/sample.py\n+++ b/src/sample.py\n@@ -1,2 +1,2 @@\n def answer() -> int:\n-    return 42\n+    return 43\n")
            bundle = base / (key + ".zip")
            freeze_bundle(source, bundle, request_key=key, task_id="1", operation_id="tools-acceptance",
                          run_id=key, script="main.py", timeout_seconds=120, memory_bytes=memory,
                          cpu_millis=cpu, profile=TOOLS_PROFILE, image=TOOLS_IMAGE)
            return bundle

        oversized = fixture(LIGHT, 1024**3, 1000)
        with oversized.open("rb") as source:
            response = http.post("/v1/jobs", content=source, headers={"Content-Length": str(oversized.stat().st_size)})
        assert response.status_code == 409 and response.json()["error"] == "BUDGET_UNAVAILABLE"
        report["one_gib_request_rejected_without_reduction"] = True

        for name, bundles in (
            ("tools_512_mib_and_light_128_mib", [fixture(SCRIPT, 512 * 1024**2, 1000, tools=True), fixture(LIGHT, 128 * 1024**2, 250)]),
            ("four_light_128_mib", [fixture(LIGHT, 128 * 1024**2, 250) for _ in range(4)]),
        ):
            jobs = [client.submit(bundle) for bundle in bundles]
            report["jobs"].extend(job.job_id for job in jobs)
            measurement = {"name": name, "requested": len(jobs), "actual_overlap": 0,
                           "host_available_min": 2**63, "oom_kills": 0, "main_memory_peak": {}}
            start = time.monotonic()
            while True:
                measurement["host_available_min"] = min(measurement["host_available_min"],
                    int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                             if line.startswith("MemAvailable:"))) * 1024)
                overlap = sum((ROOT / "jobs" / job.job_id / "work/started.txt").exists()
                              and not (ROOT / "jobs" / job.job_id / "work/finished.txt").exists() for job in jobs)
                measurement["actual_overlap"] = max(measurement["actual_overlap"], overlap)
                for job in jobs:
                    value = subprocess.run(["docker", "inspect", "sandbox-" + str(UUID(job.job_id))],
                                           capture_output=True, timeout=5, check=False)
                    if value.returncode:
                        continue
                    facts = json.loads(value.stdout)[0]
                    if not facts["State"]["Running"]:
                        continue
                    assert facts["Config"]["Image"] == TOOLS_IMAGE
                    try:
                        group = writer_group(facts["State"]["Pid"])
                        peak_path = group / "memory.peak"
                        peak = int((peak_path if peak_path.exists() else group / "memory.current").read_text())
                        measurement["main_memory_peak"][job.job_id] = max(peak, measurement["main_memory_peak"].get(job.job_id, 0))
                        events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
                        measurement["oom_kills"] = max(measurement["oom_kills"], int(events["oom_kill"]))
                    except (FileNotFoundError, ProcessLookupError):
                        pass  # A completed container may disappear during sampling.
                assert http.get("/health").status_code == 200
                if all(client.status(job.job_id).state not in {"queued", "running"} for job in jobs):
                    break
                assert time.monotonic() - start < 160
                time.sleep(.1)
            intervals = []
            for index, (job, bundle) in enumerate(zip(jobs, bundles, strict=True)):
                destination = base / job.job_id
                result = client.collect(job.job_id, bundle, destination)
                assert result.reason == "exited" and result.exit_code == 0
                assert result.profile == TOOLS_PROFILE and result.image == TOOLS_IMAGE
                assert not (destination / "main.py").exists()
                intervals.append((float((destination / "started.txt").read_text()), float((destination / "finished.txt").read_text())))
                if name.startswith("tools") and index == 0:
                    assert (destination / "src/sample.py").read_text().endswith("return 43\n")
                    assert not (destination / "approved.diff").exists()
                    report["tools_job"] = job.job_id
            assert max(a for a, _ in intervals) < min(b for _, b in intervals)
            assert measurement["actual_overlap"] == len(jobs) and measurement["oom_kills"] == 0
            assert measurement["host_available_min"] >= 256 * 1024**2
            assert len(measurement["main_memory_peak"]) == len(jobs)
            measurement["elapsed_seconds"] = time.monotonic() - start
            assert not subprocess.check_output(["docker", "ps", "-aq"]).strip()
            assert not subprocess.check_output(["docker", "volume", "ls", "-q"]).strip()
            report["capacity"].append(measurement)
            print(json.dumps({"capacity_completed": measurement}), flush=True)
    report["passed"] = True
    output = Path("/opt/yipilot-runner/toolchain-acceptance.json")
    output.write_text(json.dumps(report, indent=2) + "\n")
    output.chmod(0o600)
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
