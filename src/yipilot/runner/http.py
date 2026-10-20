"""Loopback-only authenticated Runner; request threads never own execution."""

import hmac
import json
import os
import re
import shutil
import signal
import sys
import tomllib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from filelock import FileLock
from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import MAX_BUNDLE_BYTES
from yipilot.runner.service import Runner
from yipilot.security.paths import require_plain_path


def make_server(runner: Runner, token: str, port: int) -> ThreadingHTTPServer:
    if len(token) < 32:
        raise ValueError("A separate random Runner key is required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "YiPilotRunner/2"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.handle_request()

        def do_POST(self) -> None:
            self.handle_request()

        def handle_request(self) -> None:
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                # Drain small rejected bodies so Windows can read the 401 before close.
                length = self.headers.get("Content-Length", "0")
                if (len(self.headers.get_all("Content-Length", [])) <= 1 and not self.headers.get("Transfer-Encoding")
                        and length.isascii() and length.isdecimal() and int(length) <= 64 * 1024):
                    self.connection.settimeout(1)
                    try:
                        self.rfile.read(int(length))
                    except (TimeoutError, ConnectionError):
                        return
                self.reply(401, {"error": "UNAUTHORIZED"})
                return
            try:
                if self.command == "GET" and self.path == "/health":
                    self.reply(200 if runner.healthy else 503,
                               {"status": "healthy" if runner.healthy else "unavailable", "schema_version": 2})
                elif self.command == "POST" and self.path == "/v1/jobs":
                    if not runner.healthy:
                        self.reply(503, {"error": "RUNNER_UNAVAILABLE"})
                        return
                    if (self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1
                            or self.headers.get("Content-Encoding", "identity") != "identity"):
                        raise ValueError("Unambiguous identity body length required")
                    size = int(self.headers["Content-Length"])
                    if not 0 < size <= MAX_BUNDLE_BYTES:
                        raise ValueError("Input exceeds transport budget")
                    with NamedTemporaryFile(dir=runner.staging, suffix=".zip", delete=False) as output:
                        path = Path(output.name)
                        try:
                            remaining = size
                            while remaining:
                                chunk = self.rfile.read(min(64 * 1024, remaining))
                                if not chunk:
                                    raise ValueError("Incomplete input body")
                                output.write(chunk)
                                remaining -= len(chunk)
                            output.close()
                            status = runner.submit(path)
                        finally:
                            path.unlink(missing_ok=True)
                    self.reply(202, status.model_dump(mode="json"))
                else:
                    match = re.fullmatch(r"/v1/jobs/([0-9a-f]{32})(/cancel|/result)?", self.path)
                    if match is None:
                        self.reply(404, {"error": "NOT_FOUND"})
                        return
                    job_id, suffix = match.groups()
                    if self.command == "POST" and suffix == "/cancel":
                        self.reply(200, runner.cancel(job_id).model_dump(mode="json"))
                    elif self.command == "GET" and suffix is None:
                        self.reply(200, runner.store.get(job_id)[1].model_dump(mode="json"))
                    elif self.command == "GET" and suffix == "/result":
                        path = runner.result_path(job_id)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Length", str(path.stat().st_size))
                        self.end_headers()
                        with path.open("rb") as source:
                            shutil.copyfileobj(source, self.wfile, 64 * 1024)
                    else:
                        self.reply(405, {"error": "METHOD_NOT_ALLOWED"})
            except DomainError as error:
                self.reply(404 if error.code == "JOB_NOT_FOUND" else 409, {"error": error.code})
            except (ValueError, KeyError, ValidationError, zipfile.BadZipFile, UnicodeError):
                self.reply(400, {"error": "INVALID_INPUT"})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            except Exception:  # noqa: BLE001 -- Never return SDK/config exceptions across authentication.
                self.reply(500, {"error": "RUNNER_ERROR"})

        def reply(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    from opensandbox.config import ConnectionConfig  # type: ignore[import-not-found]

    from yipilot.runner.runtime import OpenSandboxRuntime

    config_path = Path(sys.argv[1])
    require_plain_path(config_path)
    if (os.name == "nt" or getattr(os, "geteuid", lambda: -1)() != 0
            or config_path.stat().st_uid != 0 or config_path.stat().st_mode & 0o077):
        raise ValueError("Run on the Linux host with a private root configuration")
    config = tomllib.loads(config_path.read_text())
    if hmac.compare_digest(config["runner_key"], config["opensandbox_key"]):
        raise ValueError("Runner and OpenSandbox keys must be independent")
    os.umask(0o077)
    root = Path(config["root"])
    if root != Path("/srv/yipilot-runner"):
        raise ValueError("The fixed profile requires /srv/yipilot-runner")
    connection = ConnectionConfig(domain="127.0.0.1:8081", protocol="http", api_key=config["opensandbox_key"],
        use_server_proxy=True, disable_metrics=True)
    runtime = OpenSandboxRuntime(connection, disk_reserve=config["disk_reserve"])
    runner = Runner(root, runtime, max_slots=config["max_slots"], memory_budget=config["memory_budget"],
                    cpu_budget=config["cpu_budget"], disk_reserve=config["disk_reserve"])
    with FileLock(root / "runner.lock", timeout=0):
        server = make_server(runner, config["runner_key"], config["port"])
        runner.start()
        def stop(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            runner.close()


if __name__ == "__main__":
    main()
