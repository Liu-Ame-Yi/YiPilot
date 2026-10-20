"""Trusted local fixture -> authenticated Runner -> verified local reconstruction."""

import json
import secrets
import sys
import time
import zipfile
from pathlib import Path

import httpx

from yipilot.models.secrets import SecretStore
from yipilot.workspace.remote_bundle import freeze_bundle
from yipilot.workspace.runner_client import RunnerClient


def run(base_url: str, secret_ref: str, root: Path, secrets_store: SecretStore) -> dict:
    root.mkdir(mode=0o700)
    token = secrets_store.read(secret_ref)
    report = {"trusted_fixtures_only": True, "jobs": [], "checks": {}}
    with httpx.Client(base_url=base_url, trust_env=False, timeout=40,
                      headers={"Authorization": "Bearer " + token}) as http:
        client = RunnerClient(http)
        with httpx.Client(base_url=base_url, trust_env=False) as anonymous:
            assert anonymous.get("/health").status_code == 401
        assert http.get("/health").status_code == 200
        report["checks"]["authenticated_loopback_http"] = True

        cases = {
            "changes": """from pathlib import Path
import os
Path('edit.txt').write_text('changed Chinese: \\u4e2d\\u6587\\n', encoding='utf-8')
Path('new.py').write_text('print(42)\\n')
Path('delete.txt').unlink()
os.chmod('mode.py', 0o755)
print('fixture-ok')
""",
            "nonzero": "raise SystemExit(7)\n",
            "output": "import sys\nsys.stdout.write('x'*500000)\n",
            "timeout": """import subprocess, time
subprocess.Popen(['python','-c',"import pathlib,time; p=pathlib.Path('writer.txt'); [(p.write_text(str(i)),time.sleep(.05)) for i in range(1000)]"],start_new_session=True)
time.sleep(60)
""",
            "cancel": "import time\ntime.sleep(60)\n",
        }
        for name, script in cases.items():
            source = root / name
            source.mkdir()
            for path, content in {"main.py": script, "edit.txt": "old\n", "delete.txt": "old\n",
                                  "mode.py": "print(1)\n", "keep.txt": "frozen\n"}.items():
                (source / path).write_text(content, encoding="utf-8")
            bundle = root / (name + ".zip")
            request = freeze_bundle(source, bundle, request_key=secrets.token_hex(16), task_id="1",
                operation_id="runner-acceptance", run_id=name, script="main.py",
                timeout_seconds=2 if name == "timeout" else 60)
            job = client.submit(bundle)
            report["jobs"].append(job.job_id)
            assert client.submit(bundle).job_id == job.job_id
            (source / "keep.txt").write_text("changed after freeze\n")
            deadline = time.monotonic() + 100
            cancellation_sent = False
            while True:
                status = client.status(job.job_id)
                if name == "cancel" and status.sandbox_id and not cancellation_sent:
                    client.cancel(job.job_id)
                    cancellation_sent = True
                if status.state not in {"queued", "running"}:
                    break
                assert time.monotonic() < deadline, status
                time.sleep(.1)
            assert status.state in {"succeeded", "failed", "cancelled"}, status
            result = client.collect(job.job_id, bundle, root / (name + "-verified"))
            assert result.request_fingerprint == request.fingerprint and result.writers_stopped
            assert (root / (name + "-verified") / "keep.txt").read_text() == "frozen\n"
            if name == "changes":
                assert result.exit_code == 0 and result.reason == "exited"
                assert result.changed == ("edit.txt", "mode.py", "new.py") and result.deleted == ("delete.txt",)
                assert next(f for f in result.files if f.path == "mode.py").git_mode == "100755"
            elif name == "nonzero":
                assert result.exit_code == 7 and result.reason == "exited"
            elif name == "output":
                assert result.reason == "output_limit" and result.stdout_bytes == 128 * 1024
            else:
                assert result.reason == ("timed_out" if name == "timeout" else "cancelled")
            report["checks"][name] = True
            report["checks"]["deduplicated_jobs"] = True
            report["checks"]["reconstruct_from_frozen_input"] = True
        original = root / "changes.zip"
        for name in ("missing-source", "invalid-path", "digest-mismatch"):
            bad = root / (name + ".zip")
            with zipfile.ZipFile(original) as before, zipfile.ZipFile(bad, "w") as after:
                for entry in before.infolist():
                    data = before.read(entry.filename)
                    if name == "missing-source" and entry.filename == "source/main.py":
                        continue
                    if name == "invalid-path" and entry.filename == "request.json":
                        body = json.loads(data)
                        body["script"] = "../escape.py"
                        data = json.dumps(body).encode()
                    if name == "digest-mismatch" and entry.filename == "source/main.py":
                        data += b"# altered bytes\n"
                    after.writestr(entry, data)
            with bad.open("rb") as content:
                response = http.post("/v1/jobs", content=content, headers={"Content-Length": str(bad.stat().st_size)})
            assert response.status_code == 400
        report["checks"]["missing_input_digest_and_path_escape_rejected"] = True
    report["passed"] = all(report["checks"].values())
    (root / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), SecretStore()), indent=2))
