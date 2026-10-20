"""Root-only trusted fixtures: actual overlap, disk cap, disconnect and recovery."""

import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from filelock import FileLock

from yipilot.domain.remote_execution import IMAGE, PROFILE
from yipilot.runner.runtime import OpenSandboxRuntime, writer_group
from yipilot.runner.service import Runner
from yipilot.workspace.remote_bundle import freeze_bundle
from yipilot.workspace.runner_client import RunnerClient

ROOT = Path("/srv/yipilot-runner")
REPORT = Path("/opt/yipilot-runner/reliability-result.json")


def command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, timeout=30).stdout


def wait_for(predicate, seconds=100):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("Fixture did not reach expected state")


def drained():
    assert not command("docker", "ps", "-aq").strip(), "Container residue"
    assert not command("docker", "volume", "ls", "-q").strip(), "Volume residue"
    assert not any(os.path.ismount(p / "work") for p in (ROOT / "jobs").iterdir()), "Mount residue"


def available_memory():
    return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:"))) * 1024


def run():
    assert os.name == "posix" and os.geteuid() == 0
    configuration = tomllib.loads(Path("/etc/yipilot-runner.toml").read_text())
    report = {"trusted_fixtures_only": True, "capacity": [], "checks": {}, "jobs": []}
    probes = ROOT / "probes"
    probes.mkdir(mode=0o700, exist_ok=True)
    with TemporaryDirectory(dir=probes, prefix="g3-") as temporary, httpx.Client(
            base_url="http://127.0.0.1:8090", trust_env=False, timeout=40,
            headers={"Authorization": "Bearer " + configuration["runner_key"]}) as http:
        client = RunnerClient(http)
        base = Path(temporary)

        def fixture(code, name):
            key = name + "-" + secrets.token_hex(6)
            source = base / key
            source.mkdir()
            (source / "main.py").write_text(code)
            bundle = base / (key + ".zip")
            freeze_bundle(source, bundle, request_key=key, task_id="1", operation_id="runner-reliability",
                          run_id=key, script="main.py", timeout_seconds=90)
            return bundle

        def submit(bundle):
            job = client.submit(bundle)
            report["jobs"].append(job.job_id)
            return job

        def terminal(job):
            status = client.status(job.job_id)
            return status if status.state not in {"queued", "running"} else None

        def ready():
            try:
                return http.get("/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        try:
            drained()
            for count in (1, 2, 4):
                started = time.monotonic()
                bundles = [fixture("""import json,time
from pathlib import Path
payload=bytearray(48*1024**2)
begin=time.time()
Path('started.txt').write_text(str(begin))
time.sleep(12)
end=time.time()
Path('finished.txt').write_text(str(end))
print(json.dumps({'begin':begin,'end':end,'allocated':len(payload)}))
""", "capacity") for _ in range(count)]
                jobs = [submit(bundle) for bundle in bundles]
                measurement = {"requested": count, "actual_overlap": 0, "host_available_min": available_memory(),
                               "main_memory_peak": {}, "sidecar_memory_peak": {}, "oom_kills": 0,
                               "health_latency_max_seconds": 0.0, "controller_rss_peak": 0}
                while True:
                    tick = time.monotonic()
                    assert http.get("/health").status_code == 200
                    measurement["health_latency_max_seconds"] = max(measurement["health_latency_max_seconds"], time.monotonic() - tick)
                    measurement["host_available_min"] = min(measurement["host_available_min"], available_memory())
                    overlap = sum((ROOT / "jobs" / j.job_id / "work/started.txt").exists()
                                  and not (ROOT / "jobs" / j.job_id / "work/finished.txt").exists() for j in jobs)
                    measurement["actual_overlap"] = max(measurement["actual_overlap"], overlap)
                    runner_pid = command("systemctl", "show", "yipilot-runner", "--property=MainPID", "--value").decode().strip()
                    rss = int(next(line.split()[1] for line in (Path("/proc") / runner_pid / "status").read_text().splitlines()
                                   if line.startswith("VmRSS:"))) * 1024
                    measurement["controller_rss_peak"] = max(measurement["controller_rss_peak"], rss)
                    identities = command("docker", "ps", "-q").decode().split()
                    for identity in identities:
                        try:
                            facts = json.loads(command("docker", "inspect", identity))[0]
                            labels = facts["Config"]["Labels"]
                            role = "main" if labels.get("yipilot.job_id") in {j.job_id for j in jobs} else "sidecar"
                            if role == "sidecar" and labels.get("opensandbox.io/egress-sidecar-for") not in {str(UUID(j.job_id)) for j in jobs}:
                                continue
                            group = writer_group(facts["State"]["Pid"])
                            peak_path = group / "memory.peak"
                            peak = int((peak_path if peak_path.exists() else group / "memory.current").read_text())
                            peaks = measurement[role + "_memory_peak"]
                            peaks[identity] = max(peaks.get(identity, 0), peak)
                            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
                            measurement["oom_kills"] = max(measurement["oom_kills"], int(events["oom_kill"]))
                        except (FileNotFoundError, ProcessLookupError, subprocess.CalledProcessError):
                            pass  # A completed container may disappear between observations.
                    if all(terminal(job) for job in jobs):
                        break
                    assert time.monotonic() - started < 120
                    time.sleep(.1)
                for job, bundle in zip(jobs, bundles, strict=True):
                    result = client.collect(job.job_id, bundle, base / (job.job_id + "-verified"))
                    assert result.exit_code == 0 and result.reason == "exited"
                measurement["elapsed_seconds"] = time.monotonic() - started
                assert measurement["actual_overlap"] == count and measurement["oom_kills"] == 0, measurement
                assert len(measurement["main_memory_peak"]) == len(measurement["sidecar_memory_peak"]) == count
                assert measurement["host_available_min"] >= 256 * 1024**2, measurement
                drained()
                report["capacity"].append(measurement)
                print(json.dumps({"capacity_completed": measurement}), flush=True)

            bundle = fixture("""import errno
from pathlib import Path
path=Path('bounded.txt')
try:
    with path.open('wb',buffering=0) as output:
        while True: output.write(b'x'*1024**2)
except OSError as error:
    assert error.errno==errno.ENOSPC
    print('bounded_bytes='+str(path.stat().st_size))
    path.unlink()
else: raise AssertionError('Unbounded disk')
""", "quota")
            job = submit(bundle)
            assert wait_for(lambda: terminal(job)).state == "succeeded"
            result = client.collect(job.job_id, bundle, base / "quota-verified")
            assert result.exit_code == 0
            drained()
            report["checks"]["disk_enospc_isolated_and_released"] = True

            bundle = fixture("from pathlib import Path\nPath('once.txt').write_text('once')\n", "lost-submit")
            data = bundle.read_bytes()
            with socket.create_connection(("127.0.0.1", 8090), timeout=5) as channel:
                headers = ("POST /v1/jobs HTTP/1.0\r\nAuthorization: Bearer " + configuration["runner_key"]
                           + "\r\nContent-Length: " + str(len(data)) + "\r\n\r\n").encode()
                channel.sendall(headers + data)
                channel.shutdown(socket.SHUT_WR)
            time.sleep(.5)
            job = submit(bundle)
            assert wait_for(lambda: terminal(job)).state == "succeeded"
            assert client.submit(bundle).job_id == job.job_id
            with http.stream("GET", "/v1/jobs/" + job.job_id + "/result") as response:
                assert response.status_code == 200
                next(response.iter_raw(chunk_size=32))
            assert client.collect(job.job_id, bundle, base / "download-retry").exit_code == 0
            report["checks"]["lost_submit_response_and_download_retry"] = True

            with sqlite3.connect(ROOT / "jobs.db") as database:
                before = database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            with socket.create_connection(("127.0.0.1", 8090), timeout=5) as channel:
                channel.sendall(headers + data[:len(data) // 2])
                channel.shutdown(socket.SHUT_WR)
            wait_for(lambda: not list((ROOT / "staging").iterdir()))
            with sqlite3.connect(ROOT / "jobs.db") as database:
                assert database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before
            report["checks"]["interrupted_upload_not_accepted"] = True

            bundle = fixture("from pathlib import Path\nimport time\nPath('started.txt').write_text('started')\ntime.sleep(80)\n", "crash")
            job = submit(bundle)
            wait_for(lambda: (ROOT / "jobs" / job.job_id / "work/started.txt").exists())
            pid = int(command("systemctl", "show", "yipilot-runner", "--property=MainPID", "--value"))
            os.kill(pid, signal.SIGKILL)
            wait_for(ready)
            assert client.status(job.job_id).state == "uncertain"
            assert client.submit(bundle).job_id == job.job_id
            assert http.get("/v1/jobs/" + job.job_id + "/result").status_code == 409
            drained()
            report["checks"]["sigkill_running_reconciled_without_replay"] = True

            # Hold only this fixture's mount busy; failed release must keep every admission blocked.
            bundle = fixture("from pathlib import Path\nimport time\nPath('started.txt').write_text('started')\ntime.sleep(4)\n", "busy-cleanup")
            job = submit(bundle)
            work = ROOT / "jobs" / job.job_id / "work"
            wait_for(lambda: (work / "started.txt").exists())
            holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], cwd=work)
            try:
                assert wait_for(lambda: terminal(job)).state == "cleanup_failed"
                queued_bundle = fixture("print('after-reconciliation')\n", "held-budget")
                queued = submit(queued_bundle)
                time.sleep(.5)
                assert client.status(queued.job_id).state == "queued"
                assert http.get("/v1/jobs/" + job.job_id + "/result").status_code == 409
            finally:
                holder.terminate()
                holder.wait(timeout=5)
            command("systemctl", "restart", "yipilot-runner")
            wait_for(ready)
            assert client.status(job.job_id).state == "uncertain"
            assert wait_for(lambda: terminal(queued)).state == "succeeded"
            drained()
            report["checks"]["failed_release_holds_budget_and_restart_reconciles"] = True

            bundle = fixture("from pathlib import Path\nimport time\nPath('started.txt').write_text('started')\ntime.sleep(80)\n", "api-outage")
            job = submit(bundle)
            wait_for(lambda: (ROOT / "jobs" / job.job_id / "work/started.txt").exists())
            profile_pid = int(command("systemctl", "show", "opensandbox-profile", "--property=MainPID", "--value"))
            os.kill(profile_pid, signal.SIGSTOP)
            try:
                client.cancel(job.job_id)
                assert wait_for(lambda: terminal(job)).state == "cancelled"
                drained()
            finally:
                os.kill(profile_pid, signal.SIGCONT)
            report["checks"]["api_outage_uses_owned_docker_cleanup"] = True

            # Lose the create response after durable ownership, then recover through the actual service.
            from opensandbox.config import ConnectionConfig
            command("systemctl", "stop", "yipilot-runner")
            bundle = fixture("from pathlib import Path\nPath('executed.txt').write_text('unexpected replay')\n", "lost-create")
            connection = ConnectionConfig(domain="127.0.0.1:8081", protocol="http",
                api_key=configuration["opensandbox_key"], use_server_proxy=True, disable_metrics=True)
            with FileLock(ROOT / "runner.lock", timeout=0):
                runtime = OpenSandboxRuntime(connection)
                owner = Runner(ROOT, runtime)
                job = owner.submit(bundle)
                report["jobs"].append(job.job_id)
                owner.store.update(job.job_id, state="running")
                work = ROOT / "jobs" / job.job_id / "work"
                runtime.prepare_work(job.job_id, work, owner.store)
                shutil.copytree(work.parent / "input", work, dirs_exist_ok=True)
                for path in [work, *work.rglob("*")]:
                    path.chmod(0o700 if path.is_dir() else 0o644)
                    os.chown(path, 10001, 10001)
                payload = {"image": {"uri": IMAGE}, "entrypoint": ["tail", "-f", "/dev/null"], "timeout": 300,
                    "resourceLimits": {"memory": "128Mi", "cpu": "250m"},
                    "metadata": {"yipilot.job_id": job.job_id, "yipilot.profile": PROFILE},
                    "networkPolicy": {"defaultAction": "deny", "egress": [{"action": "deny", "target": "*"}]},
                    "volumes": [{"name": "work", "host": {"path": str(work)}, "mountPath": "/workspace", "readOnly": False}]}
                data = json.dumps(payload).encode()
                with socket.create_connection(("127.0.0.1", 8081), timeout=5) as channel:
                    headers = ("POST /sandboxes HTTP/1.0\r\nContent-Type: application/json\r\nOPEN-SANDBOX-API-KEY: "
                               + configuration["opensandbox_key"] + "\r\nContent-Length: " + str(len(data)) + "\r\n\r\n").encode()
                    channel.sendall(headers + data)
                    wait_for(lambda: json.loads((work.parent / "runtime.json").read_text())["state"] in {"consumed", "settled"})
            command("systemctl", "start", "yipilot-runner")
            wait_for(ready)
            assert client.status(job.job_id).state == "uncertain"
            assert client.submit(bundle).job_id == job.job_id
            assert json.loads((work.parent / "runtime.json").read_text())["state"] == "revoked"
            os.chown(work, 10001, 10001)
            with httpx.Client(trust_env=False, timeout=15) as profile_http:
                response = profile_http.post("http://127.0.0.1:8081/sandboxes", json=payload,
                    headers={"OPEN-SANDBOX-API-KEY": configuration["opensandbox_key"]})
                assert response.status_code == 400
            drained()
            report["checks"]["lost_creation_response_reconciled_and_late_create_rejected"] = True

            bundle = fixture("print('retention-fixture')\n", "retention")
            job = submit(bundle)
            assert wait_for(lambda: terminal(job)).state == "succeeded"
            command("systemctl", "stop", "yipilot-runner")
            with sqlite3.connect(ROOT / "jobs.db") as database:
                database.execute("UPDATE jobs SET finished=? WHERE id=?", (time.time() - 86401, job.job_id))
            command("systemctl", "start", "yipilot-runner")
            wait_for(ready)
            assert client.status(job.job_id).result_expired
            assert client.submit(bundle).job_id == job.job_id
            assert http.get("/v1/jobs/" + job.job_id + "/result").status_code == 410
            assert not (ROOT / "jobs" / job.job_id).exists()
            report["checks"]["restart_ttl_gc_preserves_dedup_and_returns_410"] = True
            drained()
            report["passed"] = all(report["checks"].values()) and len(report["capacity"]) == 3
        finally:
            REPORT.write_text(json.dumps(report, indent=2) + "\n")
            REPORT.chmod(0o600)
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
