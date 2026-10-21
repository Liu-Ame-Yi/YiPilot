"""Trusted-fixture acceptance on the Linux host; never executes repository code."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import docker
import httpx
import tomli
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models import Host, NetworkPolicy, Volume, WriteEntry
from opensandbox.models.execd import RunCommandOpts
from policy import DENY, IMAGE, JOBS_ROOT, PROFILE, TOOLS_IMAGE, TOOLS_PROFILE


async def run(box, code, **options):
    result = await asyncio.wait_for(box.commands.run(
        ["python", "-c", code], opts=RunCommandOpts(**options)), timeout=20)
    if result.exit_code != 0:
        raise AssertionError(f"Fixture command failed: {result.error}")
    return "".join(item.text for item in result.logs.stdout).strip()


def job_directory(job):
    root = JOBS_ROOT / job
    root.mkdir(mode=0o700)
    lease = root / "runtime.json"
    lease.write_text(json.dumps({"job_id": job, "sandbox_id": str(UUID(job)), "state": "authorized"}))
    lease.chmod(0o600)
    for name in ("input", "artifacts", "work"):
        directory = root / name
        directory.mkdir(mode=0o700)
        if name == "work":
            os.chown(directory, 10001, 10001)
        else:
            (directory / "sentinel").write_text(name)
    return root


def writer_pids(container, nonce):
    top = container.top()
    index = top["Titles"].index("PID")
    result = {}
    for row in top["Processes"]:
        path = Path("/proc") / row[index]
        try:
            if nonce.encode() in (path / "cmdline").read_bytes():
                result[int(row[index])] = (path / "stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            pass
    return result


def assert_stopped(pids):
    for pid, start in pids.items():
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            assert fields[19] != start or fields[0] == "Z", "An owned writer survived destroy"
        except FileNotFoundError:
            pass


async def main(image=IMAGE, profile=PROFILE):
    config = tomli.loads(Path("/etc/opensandbox/yipilot-profile.toml").read_text())
    client = docker.from_env()
    connection = ConnectionConfig(domain="127.0.0.1:8081", protocol="http",
        api_key=config["server"]["api_key"], use_server_proxy=True,
        disable_metrics=True, request_timeout=timedelta(seconds=25))
    checks = ("reject_before_side_effects", "creation_limits", "nonroot", "readonly", "work_roundtrip",
              "control_paths_inaccessible", "uid_zero_denied",
              "cross_job_host_metadata_and_unrestricted_peer_blocked",
              "management_secrets_absent_and_sidecar_policy_protected", "runtime_identity_immutable",
              "cancel_then_destroy_live_writers", "timeout_then_destroy_and_stable_host_collection",
              "task_capabilities_zero", "control_process_protected", "control_port_bind_denied",
              "proxy_surface_restricted", "filesystem_identity_enforced", "task_loopback_preserved")
    report = {"trusted_fixtures_only": True, "profile": profile, "jobs": [],
              "checks": dict.fromkeys(checks, False)}
    baseline_containers = {c.id for c in client.containers.list(all=True)}
    baseline_volumes = {v.id for v in client.volumes.list()}
    boxes = []
    peer = None
    try:
        # A rejected request must not create primary containers, sidecars or volumes.
        job = secrets.token_hex(16)
        root = job_directory(job)
        report["jobs"].append(job)
        payload = {"image": {"uri": image}, "entrypoint": ["tail", "-f", "/dev/null"],
            "timeout": 300, "resourceLimits": {"memory": "128Mi", "cpu": "250m"},
            "metadata": {"yipilot.job_id": job, "yipilot.profile": profile},
            "networkPolicy": DENY, "volumes": [{"name": "work", "host": {"path": str(root / "work")},
                                                "mountPath": "/workspace", "readOnly": False}]}
        before = ({c.id for c in client.containers.list(all=True)}, {v.id for v in client.volumes.list()})
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8081", timeout=30) as http:
            assert (await http.get("/sandboxes")).status_code in (401, 403)
            for field, value in [("networkPolicy", None), ("image", {"uri": "python:latest"}),
                    ("env", {"HOME": "/root"}), ("extensions", {"bootstrap.execd.isolation": "enable"}),
                    ("resourceLimits", {"cpu": "250m"}),
                    ("volumes", [{"name": "work", "host": {"path": "/etc"}, "mountPath": "/workspace"}])]:
                invalid = copy.deepcopy(payload)
                invalid[field] = value
                response = await http.post("/sandboxes", json=invalid,
                    headers={"OPEN-SANDBOX-API-KEY": config["server"]["api_key"]})
                assert response.status_code == 400, (field, response.status_code)
            # A symlink at the trusted mount point must also fail before create.
            (root / "work").rmdir()
            (root / "work").symlink_to(root / "input", target_is_directory=True)
            try:
                response = await http.post("/sandboxes", json=payload,
                    headers={"OPEN-SANDBOX-API-KEY": config["server"]["api_key"]})
                assert response.status_code == 400
            finally:
                (root / "work").unlink()
                (root / "work").mkdir(mode=0o700)
                os.chown(root / "work", 10001, 10001)
        after = ({c.id for c in client.containers.list(all=True)}, {v.id for v in client.volumes.list()})
        assert before == after
        report["checks"]["reject_before_side_effects"] = True
        for index in range(2):
            if index:
                job = secrets.token_hex(16)
                root = job_directory(job)
                report["jobs"].append(job)
            box = await asyncio.wait_for(Sandbox.create(image,
                timeout=timedelta(minutes=5), resource={"memory": "128Mi", "cpu": "250m"},
                metadata={"yipilot.job_id": job, "yipilot.profile": profile},
                entrypoint=["tail", "-f", "/dev/null"], network_policy=NetworkPolicy.model_validate(DENY),
                volumes=[Volume(name="work", host=Host(path=str(root / "work")), mount_path="/workspace")],
                connection_config=connection), timeout=70)
            boxes.append(box)
            task = client.containers.get("sandbox-" + box.id)
            sidecar = client.containers.get("sandbox-egress-" + box.id)
            task.reload()
            sidecar.reload()
            host = task.attrs["HostConfig"]
            side = sidecar.attrs["HostConfig"]
            assert task.attrs["Config"]["User"] == "0:0"
            assert host["ReadonlyRootfs"] and host["Memory"] == host["MemorySwap"] == 128 * 1024**2
            assert host["NanoCpus"] == 250_000_000 and host["PidsLimit"] == 128
            assert "ALL" in host["CapDrop"] and set(host["CapAdd"]) == {"SETUID", "SETGID", "NET_BIND_SERVICE"}
            assert not host["Privileged"]
            assert host["SecurityOpt"] == ["no-new-privileges=true"]
            assert host["LogConfig"]["Type"] == "none" and "noexec" in host["Tmpfs"]["/tmp"]
            assert side["Memory"] == side["MemorySwap"] == 64 * 1024**2
            assert side["NanoCpus"] == 100_000_000 and side["PidsLimit"] == 64
            assert side["NetworkMode"] == "yipilot-isolated"
            assert side["Sysctls"]["net.ipv4.ip_unprivileged_port_start"] == "1024"
            report["roles"] = {"container_user": task.attrs["Config"]["User"],
                               "controller_cap_add": sorted(host["CapAdd"]),
                               "task_uid": 10001, "task_gid": 10001, "task_cap_eff": 0,
                               "control_port": 443, "unprivileged_port_start": 1024}
            report["checks"]["creation_limits"] = True
        box = boxes[0]
        await box.files.write_files([WriteEntry(path="/workspace/roundtrip.txt", data="profile fixture")])
        assert await box.files.read_file("/workspace/roundtrip.txt") == "profile fixture"
        assert (JOBS_ROOT / report["jobs"][0] / "work/roundtrip.txt").stat().st_uid == 10001
        report["checks"]["filesystem_identity_enforced"] = True
        facts = json.loads(await run(box, """import json,os,pathlib,socket
assert os.getuid()==os.getgid()==10001
assert os.statvfs('/').f_flag & os.ST_RDONLY
status=pathlib.Path('/proc/self/status').read_text()
for key in ['CapEff','CapPrm','CapAmb','CapInh']:
 assert int(next(x for x in status.splitlines() if x.startswith(key+':')).split()[1],16)==0,key
assert 'NoNewPrivs:\\t1' in status
assert pathlib.Path('/proc/sys/net/ipv4/ip_unprivileged_port_start').read_text().strip()=='1024'
for path in ['/etc','/opt/opensandbox','/opt/opensandbox/execd','/var/run/docker.sock']:
 assert not os.access(path,os.W_OK),path
for path in ['/workspace/../input/sentinel','/workspace/../artifacts/sentinel','/root/.ssh']:
 assert not os.access(path,os.R_OK),path
for path in ['/opt/opensandbox/.env','/opt/opensandbox/control']:
 assert not os.access(path,os.R_OK),path
controllers=[]
for entry in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:argv=entry.read_bytes().split(b'\\0')
 except (PermissionError,FileNotFoundError,ProcessLookupError):continue
 if argv[0]==b'/opt/opensandbox/execd' and b'--filesystem-worker' not in argv:controllers.append(int(entry.parent.name))
assert len(controllers)==1,controllers
for pid in controllers:
 try:os.kill(pid,0)
 except PermissionError:pass
 else:raise AssertionError('task can signal controller')
 for name in ['environ','mem']:
  try:(pathlib.Path('/proc')/str(pid)/name).read_bytes()
  except PermissionError:pass
  else:raise AssertionError('task can read controller '+name)
try:os.setuid(0)
except PermissionError:pass
else:raise AssertionError('task changed uid')
for family in [socket.SOCK_STREAM,socket.SOCK_DGRAM]:
 s=socket.socket(socket.AF_INET,family)
 try:s.bind(('0.0.0.0',443))
 except PermissionError:pass
 else:raise AssertionError('task can bind privileged control port')
 finally:s.close()
listener=socket.socket();listener.bind(('127.0.0.1',50000));listener.listen(1)
client=socket.create_connection(listener.getsockname(),timeout=1)
accepted,_=listener.accept();accepted.close();client.close();listener.close()
try:socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)
except PermissionError:pass
else:raise AssertionError('raw socket was available')
pathlib.Path('/tmp/writable').write_text('ok')
assert pathlib.Path('/workspace/roundtrip.txt').read_text()=='profile fixture'
print(json.dumps({'nonroot':True,'readonly':True,'work_roundtrip':True,'control_paths_inaccessible':True,
 'task_capabilities_zero':True,'control_process_protected':True,'control_port_bind_denied':True,
 'task_loopback_preserved':True}))"""))
        report["checks"].update(facts)
        proxy = f"http://127.0.0.1:8081/v1/sandboxes/{box.id}/proxy/44772/"
        headers = {"OPEN-SANDBOX-API-KEY": config["server"]["api_key"]}
        async with httpx.AsyncClient(headers=headers, timeout=10) as http:
            denied = await http.post(proxy + "command", json={"argv": ["id"], "uid": 0, "gid": 0})
            assert denied.status_code == 403
            for path in ("internal/init", "code", "session", "pty", "proxy/443/internal/init",
                         "v1/filesystem/0/0/files/upload"):
                denied = await http.post(proxy + path, json={})
                assert denied.status_code == 403, path
            private = await http.get(proxy + "files/download", params={"path": "/opt/opensandbox/.env"})
            assert private.status_code != 200 and "permission" in private.text.lower()
        report["checks"]["proxy_surface_restricted"] = True
        report["checks"]["uid_zero_denied"] = True
        network = client.networks.get("yipilot-isolated")
        gateway = network.attrs["IPAM"]["Config"][0].get("Gateway")
        other = client.containers.get("sandbox-egress-" + boxes[1].id)
        other_ip = other.attrs["NetworkSettings"]["Networks"]["yipilot-isolated"]["IPAddress"]
        targets = [("169.254.169.254", 80), (other_ip, 443), (other_ip, 44772), ("1.1.1.1", 443)]
        if gateway:
            targets += [(gateway, 22), (gateway, 8080), (gateway, 8081)]
        await run(box, "import socket; targets=" + repr(targets) + "\nfor address in targets:\n s=socket.socket(); s.settimeout(.6); result=s.connect_ex(address); s.close(); assert result!=0,address\nprint('denied')")
        # An unrestricted default-bridge peer may not reach this sandbox either.
        first_sidecar = client.containers.get("sandbox-egress-" + box.id)
        destination = first_sidecar.attrs["NetworkSettings"]["Networks"]["yipilot-isolated"]["IPAddress"]
        peer = client.containers.run(image, ["python", "-c",
            f"import socket\nfor port in [443,44772]:\n s=socket.socket(); s.settimeout(1); assert s.connect_ex(({destination!r},port))!=0; s.close()"],
            detach=True, network="bridge", user="10001:10001", read_only=True,
            mem_limit="64m", memswap_limit="64m", nano_cpus=100_000_000,
            pids_limit=32, cap_drop=["ALL"], security_opt=["no-new-privileges=true"],
            labels={"yipilot.acceptance": report["jobs"][0]}, log_config={"type": "none"})
        assert peer.wait(timeout=10)["StatusCode"] == 0
        peer.remove()
        peer = None
        report["checks"]["cross_job_host_metadata_and_unrestricted_peer_blocked"] = True
        sidecar_env = dict(item.split("=", 1) for item in first_sidecar.attrs["Config"]["Env"] if "=" in item)
        secret_hashes = [hashlib.sha256(value.encode()).hexdigest() for value in
                         [config["server"]["api_key"], sidecar_env["OPENSANDBOX_EGRESS_TOKEN"]]]
        await run(box, """import pathlib,hashlib,urllib.request,urllib.error,json
hashes=HASHES
for path in list(pathlib.Path('/proc').glob('[0-9]*/environ'))+[pathlib.Path('/opt/opensandbox/.env')]:
 try:data=path.read_bytes()
 except (PermissionError,FileNotFoundError,ProcessLookupError):continue
 for item in data.replace(b'\\n',b'\\0').split(b'\\0'):
  if b'=' in item:assert hashlib.sha256(item.split(b'=',1)[1]).hexdigest() not in hashes
request=urllib.request.Request('http://127.0.0.1:18080/policy',data=json.dumps({'defaultAction':'allow'}).encode(),headers={'Content-Type':'application/json'},method='POST')
try:urllib.request.urlopen(request,timeout=2)
except urllib.error.HTTPError as exc:assert exc.code in (401,403)
except urllib.error.URLError:pass
else:raise AssertionError('task changed sidecar policy')
print('control secrets absent and policy rejected')""".replace("HASHES", repr(secret_hashes)))
        report["checks"]["management_secrets_absent_and_sidecar_policy_protected"] = True
        # Check the actual privileged control port and the old direct port.
        async with httpx.AsyncClient(headers=headers, timeout=10) as http:
            before_binding = (await http.get(proxy + "ready")).json()
        init = json.loads(await run(box, """import urllib.request,urllib.error,json
body={'sandboxId':'fixture-rebound','generation':1,'preserveRuntimeState':True,'entrypointPolicy':'keep'}
results={}
for port in [443,44772]:
 for path in ['/internal/init','/command','/files/upload','/v1/filesystem/0/0/files/upload','/pty','/session']:
  request=urllib.request.Request('http://127.0.0.1:'+str(port)+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
  try:urllib.request.urlopen(request,timeout=1)
  except urllib.error.HTTPError:raise AssertionError('task reached a control HTTP handler')
  except urllib.error.URLError:results[str(port)+path]='kernel denied'
  else:raise AssertionError('task accessed a control API')
print(json.dumps({'denied':results}))"""))
        async with httpx.AsyncClient(headers=headers, timeout=10) as http:
            after_binding = (await http.get(proxy + "ready")).json()
        assert before_binding == after_binding and before_binding.get("sandboxId") != "fixture-rebound"
        init["binding_changed"] = False
        report["init_probe"] = init
        report["checks"]["runtime_identity_immutable"] = True
        # A timed-out command's descendants are not trusted as stopped until destroy.
        nonce = secrets.token_hex(8)
        child = f"import pathlib,time; p=pathlib.Path('/workspace/{nonce}'); [(p.write_text(str(i)),time.sleep(.05)) for i in range(1200)]"
        parent = "import subprocess,time; subprocess.Popen(['python','-c'," + repr(child) + "]); time.sleep(60)"
        cancel_box = boxes[1]
        await cancel_box.commands.run(["python", "-c", parent], opts=RunCommandOpts(background=True))
        cancel_task = client.containers.get("sandbox-" + cancel_box.id)
        for _ in range(40):
            cancelling_pids = writer_pids(cancel_task, nonce)
            if len(cancelling_pids) >= 2:
                break
            await asyncio.sleep(.1)
        assert len(cancelling_pids) >= 2, "Background ack alone cannot prove that a writer ran"
        await asyncio.wait_for(cancel_box.destroy(), timeout=30)
        boxes.remove(cancel_box)
        assert_stopped(cancelling_pids)
        report["cancel_writer_count_before"] = len(cancelling_pids)
        report["checks"]["cancel_then_destroy_live_writers"] = True
        result = await box.commands.run(["python", "-c", parent], opts=RunCommandOpts(timeout=timedelta(seconds=1)))
        assert result.exit_code != 0
        task = client.containers.get("sandbox-" + box.id)
        pids = writer_pids(task, nonce)
        await asyncio.wait_for(box.destroy(), timeout=30)
        boxes.remove(box)
        assert_stopped(pids)
        work = JOBS_ROOT / report["jobs"][0] / "work"
        first = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.is_file()}
        await asyncio.sleep(.3)
        second = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.is_file()}
        assert first == second and first
        report["checks"]["timeout_then_destroy_and_stable_host_collection"] = True
    finally:
        if peer is not None:
            peer.remove(force=True)
        for box in boxes:
            await asyncio.wait_for(box.destroy(), timeout=30)
        residual = {c.id for c in client.containers.list(all=True)} - baseline_containers
        volumes = {v.id for v in client.volumes.list()} - baseline_volumes
        report["residual_containers"] = len(residual)
        report["residual_volumes"] = len(volumes)
        report["profile_acceptance_passed"] = all(report["checks"].values()) and not residual and not volumes
        report["ready_for_untrusted_code"] = False
        output = Path("/opt/opensandbox/yipilot-profile/acceptance-result.json")
        output.write_text(json.dumps(report, indent=2) + "\n")
        output.chmod(0o600)
        print(json.dumps(report, indent=2), flush=True)
        client.close()
    assert report["profile_acceptance_passed"], "Profile acceptance remains red; inspect the failed checks"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(TOOLS_IMAGE, TOOLS_PROFILE) if args.tools else main())
