from io import StringIO

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.infrastructure.secret_store import SecretStore
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.transport.cli.interactive import InteractiveSession


class ProbePort:
    def stream(self, request):
        return ()

    def close(self) -> None:
        pass


class FailingProbePort:
    def stream(self, request):
        from yipilot.domain.errors import DomainError

        raise DomainError("MODEL_CONNECTION_FAILED", "connection failed")

    def close(self) -> None:
        pass


def test_model_session_choice_is_not_persisted_and_selected_probe_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yipilot.transport.cli.interactive.ModelSelectionService", lambda path: ModelSelectionService(path, secrets=SecretStore(environ={"KEY": "secret"}, settings_dir=path), port_factory=lambda *_: ProbePort()))
    service = ModelSelectionService(tmp_path, secrets=SecretStore(environ={"KEY": "secret"}))
    service.add(ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="env:KEY", revision=1), ModelEntry(connection_id="local", model_id="coder", display_name="Coder", output_limit=100))
    output = StringIO()
    InteractiveSession(tmp_path, input=StringIO("/model\n1\n1\n1\n3\n1\n0\n0\n/exit\n"), output=output).run()
    assert "模型设置" in output.getvalue()
    assert "连接成功" in output.getvalue()
    assert ModelSelectionService(tmp_path, secrets=SecretStore(environ={"KEY": "secret"})).list_public()["default_model_selector"] is None


def test_empty_model_screen_starts_provider_picker_and_can_return(tmp_path) -> None:
    output = StringIO()
    InteractiveSession(tmp_path, input=StringIO("/model\n0\n/exit\n"), output=output).run()
    assert "欢迎配置第一个模型。" in output.getvalue()
    assert "OpenAI-compatible" in output.getvalue()
    assert "Connection ID" not in output.getvalue()
    assert ModelSelectionService(tmp_path).list_public()["models"] == []


def test_task_text_fallback_creates_selects_and_shows_durable_task_without_a_run(tmp_path) -> None:
    output = StringIO()
    InteractiveSession(
        project=tmp_path,
        input=StringIO("/task\n1\n终端任务\n验证任务向导\n3\n0\n/status\n/run\n/exit\n"),
        output=output,
    ).run()

    task = FileTaskStore(tmp_path).get_task("1")
    assert (task.title, task.development_goal) == ("终端任务", "验证任务向导")
    assert FileTaskStore(tmp_path).list_runs("1") == []
    assert "当前任务：1 · 终端任务" in output.getvalue()
    assert "最近 Run：无" in output.getvalue()
    assert "/run 尚未实现" in output.getvalue()


def test_task_can_be_selected_again_after_restart_without_model(tmp_path) -> None:
    first = InteractiveSession(
        project=tmp_path,
        input=StringIO("/task\n1\n可恢复任务\n重新选择\n0\n/exit\n"),
        output=StringIO(),
    )
    first.run()
    output = StringIO()
    InteractiveSession(
        project=tmp_path,
        input=StringIO("/task\n2\n1\n0\n/status\n/exit\n"),
        output=output,
    ).run()

    assert "已选择任务 1；选择仅保留在本次会话。" in output.getvalue()
    assert "当前任务：1 · 可恢复任务" in output.getvalue()


def test_task_selector_uses_create_current_and_back_choices(tmp_path, monkeypatch) -> None:
    session = InteractiveSession(project=tmp_path, input=StringIO("键盘任务\n验证选择器\n"), output=StringIO())
    monkeypatch.setattr(InteractiveSession, "_selector_enabled", lambda _: True)
    titles: list[str] = []
    choices = iter(["create", "current", "back"])

    def pick(self, title, text, options):
        titles.append(title)
        return next(choices)

    monkeypatch.setattr(InteractiveSession, "_pick", pick)
    session._task_screen()

    assert titles == ["任务", "任务", "任务"]
    assert session.session_task_id == "1"


def test_provider_picker_creates_and_tests_a_deepseek_model_without_asking_for_connection_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yipilot.transport.cli.interactive.ModelSelectionService", lambda path: ModelSelectionService(path, secrets=SecretStore(settings_dir=path), port_factory=lambda *_: ProbePort()))
    output = StringIO()
    InteractiveSession(tmp_path, input=StringIO("/model\n1\n1\n1\nunit-file-secret\n1\n/exit\n"), output=output).run()
    assert "DeepSeek" in output.getvalue()
    assert "Yipilot" in output.getvalue()
    assert "Connection ID" not in output.getvalue()
    assert (tmp_path / "auth.json").exists()
    assert ModelSelectionService(tmp_path, secrets=SecretStore(environ={"KEY": "secret"})).list_public()["models"][0]["selectors"] == ["deepseek-1/deepseek-v4-pro"]


def test_terminal_picker_guides_first_setup_with_keyboard_choices(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yipilot.transport.cli.interactive.ModelSelectionService", lambda path: ModelSelectionService(path, secrets=SecretStore(settings_dir=path), port_factory=lambda *_: ProbePort()))
    monkeypatch.setattr(InteractiveSession, "_selector_enabled", lambda _: True)
    monkeypatch.setattr("yipilot.transport.cli.interactive.prompt", lambda *args, **kwargs: "unit-file-secret")
    titles: list[str] = []
    choices = iter(["official", "deepseek", "deepseek-v4-pro", "back"])

    def pick(self, title, text, options):
        titles.append(title)
        return next(choices)

    monkeypatch.setattr(InteractiveSession, "_pick", pick)
    InteractiveSession(tmp_path, input=StringIO("/model\nunit-file-secret\n/exit\n"), output=StringIO()).run()
    assert titles == ["配置模型服务商", "选择官方服务商", "选择 DeepSeek 的模型", "模型已连接"]


def test_terminal_picker_esc_returns_to_the_previous_screen() -> None:
    session = InteractiveSession(input=StringIO(), output=StringIO())
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b")
        with create_app_session(input=pipe, output=DummyOutput()):
            assert session._pick("测试", "按 Esc 返回", [("ok", "确定")]) is None


def test_terminal_picker_enter_confirms_the_focused_choice() -> None:
    session = InteractiveSession(input=StringIO(), output=StringIO())
    with create_pipe_input() as pipe:
        pipe.send_text("\r\x1b")
        with create_app_session(input=pipe, output=DummyOutput()):
            assert session._pick("测试", "按 Enter 确定", [("first", "第一项")]) == "first"


def test_terminal_api_key_entry_uses_a_masked_single_line_prompt(monkeypatch) -> None:
    session = InteractiveSession(input=StringIO(), output=StringIO())
    seen: dict[str, object] = {}
    monkeypatch.setattr(InteractiveSession, "_selector_enabled", lambda _: True)

    def password_prompt(message, **kwargs):
        seen["message"] = message
        seen.update(kwargs)
        return "unit-secret"

    monkeypatch.setattr("yipilot.transport.cli.interactive.prompt", password_prompt)
    assert session._read_secret() == "unit-secret"
    assert seen["is_password"] is True
    assert "Enter" in str(seen["message"])


def test_failed_first_probe_leaves_no_model_or_auth_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yipilot.transport.cli.interactive.ModelSelectionService", lambda path: ModelSelectionService(path, port_factory=lambda *_: FailingProbePort()))
    output = StringIO()
    InteractiveSession(tmp_path, input=StringIO("/model\n1\n1\n1\nwrong-key\n0\n/exit\n"), output=output).run()
    assert "MODEL_CONNECTION_FAILED" in output.getvalue()
    assert not (tmp_path / "auth.json").exists()
    assert ModelSelectionService(tmp_path).list_public()["models"] == []


def test_custom_provider_flow_asks_for_name_url_model_then_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yipilot.transport.cli.interactive.ModelSelectionService", lambda path: ModelSelectionService(path, secrets=SecretStore(settings_dir=path), port_factory=lambda *_: ProbePort()))
    output = StringIO()
    InteractiveSession(tmp_path, input=StringIO("/model\n2\n公司网关\nhttps://gateway.example.test/v1\n1\ncompany-coder\nunit-file-secret\n0\n/exit\n"), output=output).run()
    assert "公司网关 · company-coder" in output.getvalue()
    assert ModelSelectionService(tmp_path, secrets=SecretStore(settings_dir=tmp_path)).list_public()["models"][0]["selectors"] == ["custom-1/company-coder"]


def test_manage_models_deletes_last_model_connection_and_file_key(tmp_path, monkeypatch) -> None:
    secrets = SecretStore(settings_dir=tmp_path)
    secrets.set_file("local", "unit-secret")
    service = ModelSelectionService(tmp_path, secrets=secrets, port_factory=lambda *_: ProbePort())
    service.add(ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="file:local", revision=1), ModelEntry(connection_id="local", model_id="coder", display_name="Coder", output_limit=100))
    monkeypatch.setattr("yipilot.transport.cli.interactive.ModelSelectionService", lambda path: service)
    InteractiveSession(tmp_path, input=StringIO("/model\n3\nd\n1\ny\n0\n/exit\n"), output=StringIO()).run()
    assert service.list_public()["models"] == []
    assert not (tmp_path / "auth.json").exists()
