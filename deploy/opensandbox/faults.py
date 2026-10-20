"""Trusted Linux fixtures for control setup failure and controller death."""

import asyncio
import json
import os
import secrets
import signal
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import docker
import tomli
from acceptance import job_directory, writer_pids
from fastapi import HTTPException
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models import Host, NetworkPolicy, Volume
from opensandbox.models.execd import RunCommandOpts
from policy import DENY, IMAGE, PROFILE


async def failed_guard(report):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from opensandbox_server.config import load_config

    root = Path("/opt/opensandbox/probes") / ("failed-guard-" + secrets.token_hex(12))
    root.mkdir(mode=0o700)
    config_text = Path("/etc/opensandbox/yipilot-profile.toml").read_text()
    config_text = config_text.replace("/var/lib/opensandbox/yipilot-profile.db", str(root / "state.db"))
    config_path = root / "config.toml"
    config_path.write_text(config_text)
    config_path.chmod(0o600)
    configuration = load_config(config_path)
    from opensandbox_server.api.schema import CreateSandboxRequest
    from opensandbox_server.services.docker.docker_service import DockerSandboxService

    service = DockerSandboxService(configuration)
    job = secrets.token_hex(16)
    directory = job_directory(job)
    request = CreateSandboxRequest.model_validate({
        "image": {"uri": IMAGE}, "entrypoint": ["tail", "-f", "/dev/null"], "timeout": 180,
        "resourceLimits": {"cpu": "250m", "memory": "128Mi"},
        "metadata": {"yipilot.job_id": job, "yipilot.profile": PROFILE}, "networkPolicy": DENY,
        "volumes": [{"name": "work", "host": {"path": str(directory / "work")}, "mountPath": "/workspace"}],
    })
    observed = {}

    def reject(sidecar, network):
        sidecar.reload()
        assert sidecar.attrs["State"]["Running"] and network == "yipilot-isolated"
        observed["sidecar_started"] = True
        raise OSError("Trusted fixture: control guard setup failed")

    try:
        with patch("opensandbox_server.services.docker.networking.protect_namespace", side_effect=reject), \
                patch.object(service, "_create_and_start_container", wraps=service._create_and_start_container) as start:
            try:
                await asyncio.wait_for(service.create_sandbox(request), timeout=45)
            except HTTPException as exc:
                assert exc.status_code == 500
            else:
                raise AssertionError("Guard failure was accepted")
            assert observed.get("sidecar_started") and not start.called
        report["guard_failure_before_task_start"] = True
        assert not service.docker_client.containers.list(all=True)
        assert not service.docker_client.volumes.list()
        report["guard_failure_resources_cleaned"] = True
    finally:
        service.docker_client.close()
        config_path.unlink()


async def controller_death(report):
    configuration = tomli.loads(Path("/etc/opensandbox/yipilot-profile.toml").read_text())
    connection = ConnectionConfig(domain="127.0.0.1:8081", protocol="http",
        api_key=configuration["server"]["api_key"], use_server_proxy=True,
        disable_metrics=True, request_timeout=timedelta(seconds=25))
    job = secrets.token_hex(16)
    root = job_directory(job)
    client = docker.from_env()
    box = None
    nonce = secrets.token_hex(8)
    try:
        box = await Sandbox.create(IMAGE, timeout=timedelta(minutes=3),
            resource={"cpu": "250m", "memory": "128Mi"},
            metadata={"yipilot.job_id": job, "yipilot.profile": PROFILE},
            entrypoint=["tail", "-f", "/dev/null"], network_policy=NetworkPolicy.model_validate(DENY),
            volumes=[Volume(name="work", host=Host(path=str(root / "work")), mount_path="/workspace")],
            connection_config=connection)
        child = """import json,pathlib,socket,time,os
root=pathlib.Path('/workspace')
(root/'armed-NONCE').write_text('ready')
for _ in range(100):
 if (root/'go-NONCE').exists():break
 time.sleep(.1)
else:raise AssertionError('controller death fixture was never armed')
caps=int(next(x for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('CapEff:')).split()[1],16)
assert caps==0 and os.getuid()==10001
s=socket.socket()
try:s.bind(('0.0.0.0',443))
except PermissionError:blocked=True
else:blocked=False
finally:s.close()
(root/'proof-NONCE').write_text(json.dumps({'task_uid':os.getuid(),'cap_eff':caps,'control_bind_denied_after_death':blocked}))
""".replace("NONCE", nonce)
        parent = "import subprocess,time; subprocess.Popen(['python','-c'," + repr(child) + \
                 "],start_new_session=True); time.sleep(60)"
        await box.commands.run(["python", "-c", parent], opts=RunCommandOpts(background=True))
        armed = root / "work" / ("armed-" + nonce)
        for _ in range(40):
            if armed.exists():
                break
            await asyncio.sleep(.1)
        assert armed.read_text() == "ready"
        container = client.containers.get("sandbox-" + box.id)
        container.reload()
        assert container.labels["yipilot.job_id"] == job
        top = container.top()
        index = top["Titles"].index("PID")
        controllers = []
        for row in top["Processes"]:
            pid = int(row[index])
            try:
                argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if argv[0] == b"/opt/opensandbox/execd" and b"--filesystem-worker" not in argv:
                controllers.append(pid)
        assert len(controllers) == 1
        pid = controllers[0]
        descriptor = os.pidfd_open(pid)
        try:
            assert container.id in (Path("/proc") / str(pid) / "cgroup").read_text()
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        finally:
            os.close(descriptor)
        (root / "work" / ("go-" + nonce)).write_text("controller killed")
        proof = root / "work" / ("proof-" + nonce)
        for _ in range(60):
            if proof.exists():
                break
            await asyncio.sleep(.1)
        assert proof.exists(), {"surviving_fixture_processes": len(writer_pids(container, nonce))}
        observed = json.loads(proof.read_text())
        assert observed == {"task_uid": 10001, "cap_eff": 0, "control_bind_denied_after_death": True}
        report["controller_death_takeover"] = observed
    finally:
        if box is not None:
            await asyncio.wait_for(box.destroy(), timeout=30)
        client.close()


async def main():
    client = docker.from_env()
    assert not client.containers.list(all=True) and not client.volumes.list(), "Run fixtures with no active jobs"
    report = {"trusted_fixtures_only": True, "profile": PROFILE}
    try:
        await failed_guard(report)
        await controller_death(report)
    finally:
        report["residual_containers"] = len(client.containers.list(all=True))
        report["residual_volumes"] = len(client.volumes.list())
        Path("/opt/opensandbox/yipilot-profile/control-faults-result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        client.close()
    assert report.get("guard_failure_before_task_start") and report.get("guard_failure_resources_cleaned")
    assert report["controller_death_takeover"]["control_bind_denied_after_death"]
    assert report["residual_containers"] == report["residual_volumes"] == 0


if __name__ == "__main__":
    asyncio.run(main())
