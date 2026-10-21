"""Real remote apply -> failing pytest -> bounded repair -> fresh verified handoff.

Only the model is a trusted fixture. Every execution uses the configured Runner.
The source repository and task worktrees are disposable, nonsensitive fixtures.
"""

import json
import subprocess
from pathlib import Path

import httpx

from yipilot.app import ProjectApplication
from yipilot.domain.model import ModelChunk
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.domain.remote_execution import TOOLS_IMAGE, TOOLS_PROFILE
from yipilot.models.secrets import SecretStore
from yipilot.models.selection import ModelSelectionService
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.workspace.runner_client import RunnerClient


class FixtureModel:
    def __init__(self):
        self.calls = 0

    def stream(self, request):
        self.calls += 1
        value = 3 if "Bounded OpenSandbox repair" in request.user_instruction else 2
        proposal = {"summary": "Update fixture value", "patches": [{"path": "src/value.py",
            "unified_diff": f"--- a/src/value.py\n+++ b/src/value.py\n@@ -1 +1 @@\n-value: int = 1\n+value: int = {value}\n"}]}
        return (ModelChunk(content=json.dumps(proposal)),)

    def close(self):
        pass


class FixtureModels(ModelSelectionService):
    def __init__(self, settings_dir):
        super().__init__(settings_dir)
        self.fixture = FixtureModel()

    def model_port(self, resolved):
        return self.fixture


def run(endpoint: str, secret_ref: str, root: Path, secrets_store: SecretStore) -> dict:
    root.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir()
    for arguments in (("init", "--initial-branch=main"), ("config", "user.name", "Fixture"),
                      ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", *arguments], cwd=source, check=True, capture_output=True)
    (source / "src").mkdir()
    (source / "tests").mkdir()
    (source / "src/value.py").write_text("value: int = 1\n", encoding="utf-8")
    (source / "tests/test_value.py").write_text("from value import value\n\n\ndef test_value() -> None:\n    assert value == 3\n", encoding="utf-8")
    (source / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
    project = root / "project"
    project.mkdir()
    models = FixtureModels(root / "settings")
    models.add(ProviderConnection(connection_id="fixture", base_url="https://example.invalid/v1",
        timeout_seconds=10, credential_ref="env:FIXTURE_MODEL_KEY", revision=1),
        ModelEntry(connection_id="fixture", model_id="fixture", display_name="Fixture", output_limit=4096))
    models.set_default("fixture/fixture")
    approvals = []

    def confirm(prompt):
        for request in prompt.requests:
            assert request.schema_version == 2 and request.profile == TOOLS_PROFILE
            assert request.image_digest == TOOLS_IMAGE and request.runner_endpoint == endpoint
            assert request.budgets.memory_bytes == 512 * 1024**2
        approvals.append({"run_id": prompt.run_id, "action": prompt.action, "fingerprint": prompt.fingerprint})
        return True

    token = secrets_store.read(secret_ref)
    with httpx.Client(base_url=endpoint, trust_env=False, timeout=40,
                      headers={"Authorization": "Bearer " + token}) as http:
        client = RunnerClient(http)
        assert client.doctor()["status"] == "healthy"
        app = ProjectApplication(project, models=models, runner=client)
        app.tasks.initialize_project()
        app.tasks.create_interactive_task("fixture", "Make fixture tests pass")
        app.repos.add_and_sync("1", str(source), "repo")
        generated = app.generate("1", "repo", "Update fixture value", context_files=("src/value.py",))
        assert generated.status == "succeeded"

        def apply_proposal(run_id):
            proposal = next(p for p in app.proposals("1") if p["proposal_run_id"] == run_id)
            result = app.apply("1", run_id, proposal_hash=proposal["proposal_hash"], confirm=confirm)
            assert result is not None
            return result

        initial = apply_proposal(generated.run_id)
        failed = app.verify("1", initial, confirm=confirm)
        assert failed.error is not None and failed.handoff is None
        assert failed.repair is not None and failed.repair.status == "succeeded" and failed.repair_error is None
        repaired = apply_proposal(failed.repair.run_id)
        verified = app.verify("1", repaired, confirm=confirm)
        assert verified.handoff is not None and verified.error is None
        fresh = ProjectApplication(project, models=models, runner=client)
        fresh.sandbox.accepted("1", repaired)
        FileArtifactStore(fresh.sandbox.store).verify("1", verified.handoff.run_id, verified.handoff.artifact_id)
        assert (source / "src/value.py").read_text() == "value: int = 1\n"
        worktree = project / ".yipilot/tasks/1/repos/repo/workspaces" / repaired
        assert (worktree / "src/value.py").read_text() == "value: int = 3\n"
        assert models.fixture.calls == 2
        jobs = []
        for record in app.sandbox.store.list_runs("1"):
            path = app.sandbox.store.event_journal_path(record.run_id).parent / "remote-job/result.json"
            if path.exists():
                result = json.loads(path.read_bytes())
                assert result["schema_version"] == 2 and result["writers_stopped"] and result["resources_removed"]
                assert client.status(result["job_id"]).state in {"succeeded", "failed"}
                jobs.append({"run_id": record.run_id, "stage": record.stage, "job_id": result["job_id"],
                             "exit_code": result["exit_code"], "request_fingerprint": result["request_fingerprint"]})
        assert len(jobs) == 6 and sum(job["exit_code"] != 0 for job in jobs) == 1
        assert not any(r.status.value in {"pending", "running", "paused"} for r in app.sandbox.store.list_runs("1"))
    report = {"passed": True, "trusted_fixture_model_only": True, "actual_remote_runtime": True,
        "profile": TOOLS_PROFILE, "image": TOOLS_IMAGE, "initial_apply_run": initial, "repair_apply_run": repaired,
        "handoff": verified.handoff.model_dump(mode="json"), "approvals": approvals, "jobs": jobs,
        "source_unchanged": True, "new_worktree_value": 3}
    (root / "business-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
