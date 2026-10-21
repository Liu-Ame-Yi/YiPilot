"""Versioned remote Jobs; independent of historical Windows Docker evidence."""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from yipilot.domain.execution import Sha256, TaskId
from yipilot.domain.provider import parse_api_key_ref
from yipilot.domain.sandbox import Identifier, SourceFile, source_path

PROFILE: Literal["yipilot-python-v2"] = "yipilot-python-v2"
IMAGE = "public.ecr.aws/docker/library/python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258"
TOOLS_PROFILE: Literal["yipilot-tools-v3"] = "yipilot-tools-v3"
TOOLS_IMAGE = "sha256:caac41fa96e695ef735b8583eb975b74a21567c234ddb7d789e58df13389b755"
Profile = Literal["yipilot-python-v2", "yipilot-tools-v3"]
IMAGES = {PROFILE: IMAGE, TOOLS_PROFILE: TOOLS_IMAGE}
ScriptPolicy = Literal["python-script-v1", "python-isolated-script-v2"]
MAX_BUNDLE_BYTES = 132 * 1024**2
MAX_LOG_BYTES = 128 * 1024
MAX_DIAGNOSTIC_BYTES = 12 * MAX_LOG_BYTES + 16384
SandboxId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


def canonical(model: BaseModel) -> bytes:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode()


def manifest_hash(files: tuple[SourceFile, ...]) -> str:
    return hashlib.sha256(json.dumps([f.model_dump() for f in files], sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class RemoteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunnerSettings(RemoteModel):
    schema_version: Literal[1] = 1
    endpoint: str = Field(min_length=1, max_length=2048)
    credential_ref: str

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        url = urlsplit(self.endpoint)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None
                or url.path or url.query or url.fragment or self.endpoint.endswith("/")
                or url.geturl() != self.endpoint or any(ord(c) < 32 or ord(c) == 127 for c in self.endpoint)
                or (url.scheme == "http" and url.hostname != "127.0.0.1")):
            raise ValueError("Use HTTPS or a loopback SSH tunnel without URL credentials or path")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("Invalid Runner port")
        parse_api_key_ref(self.credential_ref)
        return self


class JobRequest(RemoteModel):
    schema_version: Literal[2] = 2
    request_key: Identifier
    task_id: TaskId
    operation_id: Identifier
    run_id: Identifier
    policy_revision: ScriptPolicy = "python-script-v1"
    profile: Profile = PROFILE
    image: str = IMAGE
    script: str
    files: tuple[SourceFile, ...] = Field(min_length=1, max_length=10000)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    memory_bytes: int = Field(default=128 * 1024**2, ge=128 * 1024**2, le=1024**3)
    cpu_millis: int = Field(default=250, ge=100, le=1000)
    stdout_bytes: int = Field(default=MAX_LOG_BYTES, ge=1, le=MAX_LOG_BYTES)
    stderr_bytes: int = Field(default=MAX_LOG_BYTES, ge=1, le=MAX_LOG_BYTES)

    _script = field_validator("script")(source_path)
    _identifiers = field_validator("request_key", "operation_id", "run_id")(source_path)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        validate_files(self.files)
        if self.image != IMAGES[self.profile] or not self.script.endswith(".py") or self.script not in {f.path for f in self.files}:
            raise ValueError("A pinned image and a registered input Python script are required")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(self)).hexdigest()


def validate_files(files: tuple[SourceFile, ...]) -> None:
    paths = [f.path for f in files]
    if paths != sorted(paths) or len({p.casefold() for p in paths}) != len(paths):
        raise ValueError("File paths must be sorted and unique")
    if len(files) > 10000 or sum(f.size_bytes for f in files) > 128 * 1024**2:
        raise ValueError("File tree exceeds budget")
    if any(f.size_bytes > 16 * 1024**2 for f in files):
        raise ValueError("File exceeds budget")
    if any(len(part.encode("utf-8")) > 255 for f in files for part in f.path.split("/")):
        raise ValueError("Path component exceeds portable filesystem limits")


JobState = Literal["queued", "running", "succeeded", "failed", "cancelled", "uncertain", "cleanup_failed"]


class JobStatus(RemoteModel):
    schema_version: Literal[2] = 2
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_fingerprint: Sha256
    state: JobState
    cancel_requested: bool = False
    sandbox_id: SandboxId | None = None
    error_code: str | None = None
    result_expired: bool = False


class JobDiagnostics(RemoteModel):
    """Bounded execution observations; never a verified source/result receipt."""

    schema_version: Literal[1] = 1
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_fingerprint: Sha256
    sandbox_id: SandboxId | None
    error_code: Literal["OUTPUT_INVALID"] = "OUTPUT_INVALID"
    failure_stage: Literal["output_collection"] = "output_collection"
    reason: Literal["exited", "cancelled", "timed_out", "output_limit", "runtime_error"]
    exit_code: int | None
    started_at: datetime
    ended_at: datetime
    stdout: str = Field(max_length=MAX_LOG_BYTES)
    stderr: str = Field(max_length=MAX_LOG_BYTES)

    @field_validator("stdout", "stderr")
    @classmethod
    def validate_log(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_LOG_BYTES:
            raise ValueError("Diagnostic log exceeds byte budget")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None or self.ended_at < self.started_at:
            raise ValueError("Invalid diagnostic interval")
        return self


class JobResult(RemoteModel):
    schema_version: Literal[2] = 2
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_fingerprint: Sha256
    input_manifest_hash: Sha256
    output_manifest_hash: Sha256
    sandbox_id: SandboxId
    profile: Profile = PROFILE
    image: str = IMAGE
    task_uid: Literal[10001] = 10001
    task_gid: Literal[10001] = 10001
    reason: Literal["exited", "cancelled", "timed_out", "output_limit", "runtime_error"]
    exit_code: int | None
    started_at: datetime
    ended_at: datetime
    writers_stopped: Literal[True] = True
    resources_removed: Literal[True] = True
    files: tuple[SourceFile, ...]
    deleted: tuple[str, ...]
    changed: tuple[str, ...]
    stdout_sha256: Sha256
    stderr_sha256: Sha256
    stdout_bytes: int = Field(ge=0, le=MAX_LOG_BYTES)
    stderr_bytes: int = Field(ge=0, le=MAX_LOG_BYTES)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        validate_files(self.files)
        if self.image != IMAGES[self.profile] or self.output_manifest_hash != manifest_hash(self.files):
            raise ValueError("Output manifest or image differs")
        if (self.started_at.tzinfo is None or self.ended_at.tzinfo is None
                or self.ended_at < self.started_at or (self.reason == "exited" and self.exit_code is None)):
            raise ValueError("Invalid execution outcome")
        for paths in (self.changed, self.deleted):
            if list(paths) != sorted(set(paths)):
                raise ValueError("Change lists must be canonical")
            for path in paths:
                source_path(path)
        return self
