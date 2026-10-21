"""Guard the implemented package ownership, including non-empty scan roots."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3] / "src/yipilot"


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_core_never_imports_transport_and_workspace_never_orchestrates():
    for package in ("workflow", "agent", "workspace", "models", "domain", "persistence", "harness", "security"):
        paths = list((ROOT / package).rglob("*.py"))
        assert any(path.name != "__init__.py" for path in paths), package
        for path in paths:
            imports = tuple(imported_modules(path))
            assert not any(m.startswith("yipilot.transport") for m in imports), path
            if package == "workspace":
                assert not any(m == "yipilot.app" or m.startswith(("yipilot.workflow", "yipilot.agent")) for m in imports), path
            if package == "agent":
                assert not any(m == "yipilot.app" or m.startswith(("yipilot.workflow", "yipilot.workspace", "yipilot.models", "yipilot.persistence")) for m in imports), path
            if package in {"models", "persistence", "security", "domain"}:
                assert not any(m == "yipilot.app" or m.startswith(("yipilot.workflow", "yipilot.workspace", "yipilot.agent")) for m in imports), path


def test_terminal_submits_intent_without_approvals_or_docker_orchestration():
    forbidden = {"ApprovalRecord", "FileApprovalStore", "CodeService", "RepairService",
                 "SandboxCliService", "SandboxOutputService", "SandboxApplyService",
                 "SandboxVerificationService", "DockerSandboxWorker", "RunnerSandboxWorker"}
    for path in (ROOT / "transport/cli").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} & forbidden, path
    for path, name in [(ROOT / "transport/cli/app.py", "code_generate"),
                       (ROOT / "transport/cli/interactive.py", "_run_live_proposal")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        assert any(isinstance(n, ast.Attribute) and n.attr == "generate" for n in ast.walk(method))


def test_compatibility_facades_and_cross_module_private_imports_are_gone():
    for package in ("application", "runtime", "infrastructure"):
        assert not list((ROOT / package).rglob("*.py")), package
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("yipilot"):
                assert not [n.name for n in node.names if n.name.startswith("_") and n.name != "__version__"], path
        assert not any(m.startswith(("yipilot.application", "yipilot.runtime", "yipilot.infrastructure")) for m in imported_modules(path)), path
    tree = ast.parse((ROOT / "workflow/run_executor.py").read_text(encoding="utf-8"))
    assert not any(n.args.vararg or n.args.kwarg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
