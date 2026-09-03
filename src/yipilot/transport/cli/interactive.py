"""Small stdio-only terminal transport for future-run model selection."""

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import choice, prompt
from prompt_toolkit.styles import Style

from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.application.project_service import ProjectService
from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.infrastructure.secret_store import SecretStore
from yipilot.infrastructure.task_store import TaskRecord


@dataclass(frozen=True)
class _ProviderOption:
    identifier: str
    name: str
    base_url: str | None
    models: tuple[tuple[str, str], ...]


_PROVIDERS = (
    _ProviderOption(
        "deepseek", "DeepSeek", "https://api.deepseek.com",
        (("deepseek-v4-pro", "DeepSeek V4 Pro"), ("deepseek-v4-flash", "DeepSeek V4 Flash")),
    ),
    _ProviderOption(
        "openai", "OpenAI", "https://api.openai.com/v1",
        (("gpt-5", "GPT-5"),),
    ),
    _ProviderOption(
        "bailian", "阿里云百炼（千问）", None,
        (("qwen-plus", "Qwen Plus"),),
    ),
)

_SELECTOR_STYLE = Style.from_dict({
    "frame.border": "#14b8a6",
    "option": "#cbd5e1",
    "selected-option": "#5eead4 bold",
    "number": "#64748b",
    "bottom-toolbar": "bg:#172033 #94a3b8",
    "bottom-toolbar.text": "bg:#172033 #94a3b8",
})


@dataclass
class InteractiveSession:
    settings_dir: Path | None = None
    project: Path | None = None
    input: TextIO | None = None
    output: TextIO | None = None

    def __post_init__(self) -> None:
        import sys

        self.input = self.input or sys.stdin
        self.output = self.output or sys.stdout
        self.models = ModelSelectionService(self.settings_dir)
        self.tasks = ProjectService(self.project or Path("."))
        self.session_selector: str | None = None
        self.selected_selector: str | None = None
        self.session_task_id: str | None = None

    def run(self) -> None:
        self._write(self._status_line())
        self._write("输入 /help 查看可用命令。")
        while True:
            self._write("Yipilot> ", end="")
            line = self._readline()
            if not line:
                self._write("交互输入已结束。")
                return
            command = line.strip()
            if command == "/exit":
                self._write("已退出 Yipilot。")
                return
            if command == "/help":
                self._write("/task：创建或选择任务；/model：为未来 Run 选择模型；/status：查看当前选择；/exit：退出。")
                self._write("/run、/logs、/cancel 当前不可用；/run 尚未实现。")
            elif command == "/status":
                self._write(self._status_line())
                self._show_current_task()
            elif command == "/model":
                self._model_screen()
            elif command == "/task":
                self._task_screen()
            elif command == "/run":
                self._write("/run 尚未实现；当前不会创建或启动 Run。")
            elif command in {"/logs", "/cancel"}:
                self._write(f"{command} 当前不可用。")
            elif command:
                self._write("未知命令。输入 /help 查看帮助。")

    def _status_line(self) -> str:
        try:
            default = self.models.list_public()["default_model_selector"]
        except DomainError:
            default = None
        chosen = self.session_selector or default or "未选择"
        project = str(self.project) if self.project else "."
        task = self.session_task_id or "无"
        return f"Yipilot | 项目: {project} | 任务: {task} | 模型: {chosen} | 状态: 空闲"

    def _task_screen(self) -> None:
        while True:
            tasks = self._task_listing()
            if tasks is None:
                return
            if self._selector_enabled():
                choice = self._pick(
                    "任务",
                    f"当前选择：{self.session_task_id or '未选择'}\n\n↑/↓ 选择 · Enter 确定 · Esc 返回",
                    [
                        ("create", "创建任务"),
                        ("select", "选择已有任务"),
                        ("current", "查看当前任务"),
                        ("back", "返回"),
                    ],
                )
            else:
                self._write("任务")
                self._write(f"当前选择：{self.session_task_id or '未选择'}")
                self._write("1. 创建任务")
                self._write("2. 选择已有任务")
                self._write("3. 查看当前任务")
                self._write("0. 返回")
                self._write("请选择：", end="")
                choice = {"1": "create", "2": "select", "3": "current", "0": "back"}.get(
                    self._readline().strip()
                )
            if choice in {None, "back"}:
                return
            if choice == "create":
                self._create_task()
            elif choice == "select":
                self._select_task(tasks)
            else:
                self._show_current_task()

    def _task_listing(self) -> list[TaskRecord] | None:
        try:
            return self.tasks.list_tasks()
        except DomainError as error:
            if error.code == "PROJECT_NOT_INITIALIZED":
                return []
            self._error(error)
            return None

    def _create_task(self) -> None:
        title = self._configuration_value("任务标题")
        goal = self._configuration_value("开发目标") if title is not None else None
        if title is None or goal is None:
            return
        try:
            task = self.tasks.create_interactive_task(title, goal)
            self.session_task_id = task.task_id
            self._write(f"已创建并选择任务 {task.task_id}；未创建或启动 Run。")
        except DomainError as error:
            self._error(error)

    def _select_task(self, tasks: list[TaskRecord]) -> None:
        if not tasks:
            self._write("还没有任务；请选择“创建任务”。")
            return
        rows = [(task.task_id, self._task_label(task)) for task in tasks]
        if self._selector_enabled():
            task_id = self._pick(
                "选择已有任务",
                "↑/↓ 选择 · Enter 确定 · Esc 返回",
                rows,
            )
        else:
            self._write("选择已有任务")
            for index, (_, label) in enumerate(rows, 1):
                self._write(f"{index}. {label}")
            self._write("0. 返回")
            self._write("请选择：", end="")
            choice = self._readline().strip()
            task_id = rows[int(choice) - 1][0] if choice.isdigit() and 1 <= int(choice) <= len(rows) else None
        if task_id is not None:
            self.session_task_id = task_id
            self._write(f"已选择任务 {task_id}；选择仅保留在本次会话。")

    @staticmethod
    def _task_label(task: TaskRecord) -> str:
        return f"{task.task_id} · {task.title or '未命名任务'}"

    def _show_current_task(self) -> None:
        if self.session_task_id is None:
            self._write("当前任务：未选择。")
            return
        try:
            task = self.tasks.show_task(self.session_task_id)
            runs = self.tasks.list_runs(task.task_id)
        except DomainError as error:
            self._error(error)
            return
        title = task.title or "未命名任务"
        self._write(f"当前任务：{task.task_id} · {title}")
        self._write(f"开发目标：{task.development_goal or '未记录'}")
        if not runs:
            self._write("任务状态：尚无 Run")
            self._write("最近 Run：无")
            return
        recent = runs[-1]
        self._write(f"任务状态：{recent.status}")
        self._write(f"最近 Run：{recent.stage} · {recent.status} · {recent.run_id}")

    def _model_screen(self) -> None:
        if self._selector_enabled():
            self._model_selector_screen()
            return
        listing = self._listing()
        if listing is None:
            return
        if not self._selectors(listing):
            self._write("欢迎配置第一个模型。完成连接、密钥与模型信息后，即可开始 live Run。")
            self._configure()
            return
        while True:
            listing = self._listing()
            if listing is None:
                return
            self._write("模型设置")
            self._write(f"当前会话选择：{self._model_label(self.session_selector) if self.session_selector else '无'}")
            default = cast(str | None, listing["default_model_selector"])
            self._write(f"全局默认模型：{self._model_label(default) if default else '尚未设置'}")
            self._write("1. 选择已配置的模型")
            self._write("2. 添加新的模型服务商")
            self._write("3. 查看或测试已有模型")
            self._write("0. 返回")
            self._write("请选择：", end="")
            choice = self._readline().strip()
            if not choice or choice == "0":
                return
            if choice == "1":
                self._select_model(self._selectors(listing))
                continue
            if choice == "2":
                self._configure()
                continue
            if choice == "3":
                self._inspect_models(self._selectors(listing))
                continue
            self._write("请输入 0、1、2 或 3。")

    def _model_selector_screen(self) -> None:
        while True:
            listing = self._listing()
            if listing is None:
                return
            rows = self._selectors(listing)
            if not rows:
                self._configure()
                return
            default = cast(str | None, listing["default_model_selector"])
            choice = self._pick(
                "模型设置",
                "当前会话："
                f"{self._model_label(self.session_selector) if self.session_selector else '未选择'}\n"
                f"全局默认：{self._model_label(default) if default else '未设置'}\n\n"
                "↑/↓ 选择 · Enter 确定 · Esc 返回",
                [
                    ("choose", "选择已配置的模型"),
                    ("add", "添加模型服务商"),
                    ("manage", "测试或删除已有模型"),
                    ("back", "返回"),
                ],
            )
            if choice in {None, "back"}:
                return
            if choice == "choose":
                self._select_model(rows)
            elif choice == "add":
                self._configure()
            else:
                self._inspect_models(rows)

    def _listing(self) -> dict[str, object] | None:
        try:
            return self.models.list_public()
        except DomainError as error:
            self._error(error)
            return None

    @staticmethod
    def _selectors(listing: dict[str, object]) -> list[str]:
        rows: list[str] = []
        for item in cast(list[dict[str, object]], listing["models"]):
            rows.extend(cast(list[str], item["selectors"]))
        return rows

    def _select_model(self, rows: list[str]) -> None:
        if self._selector_enabled():
            selector = self._pick(
                "选择模型",
                "选择后可仅用于当前终端，或保存为全局默认。\n\n↑/↓ 选择 · Enter 确定 · Esc 返回",
                [(selector, self._model_label(selector)) for selector in rows],
            )
            if selector is not None:
                self._choose(selector)
            return
        while True:
            self._write("选择模型")
            self._write("选择后可仅用于本次会话，或保存为全局默认。")
            self._write_model_rows(rows)
            self._write("N. 新增模型连接  L. 清空会话选择  0. 返回")
            self._write("请选择：", end="")
            choice = self._readline().strip()
            if not choice or choice == "0":
                return
            if choice.casefold() == "n":
                self._configure()
                return
            if choice.casefold() == "l":
                self.session_selector = None
                self._write("已清空会话选择。")
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(rows):
                self._choose(rows[int(choice) - 1])
                return
            self._write("请选择列表中的编号、N、L 或 0。")

    def _inspect_models(self, rows: list[str]) -> None:
        if self._selector_enabled():
            selector = self._pick(
                "管理已有模型",
                "选择模型后可以测试连接或删除本地配置。\n\n↑/↓ 选择 · Enter 确定 · Esc 返回",
                [(selector, self._model_label(selector)) for selector in rows],
            )
            if selector is None:
                return
            action = self._pick(
                self._model_label(selector),
                "选择要执行的操作。",
                [("test", "测试连接"), ("delete", "删除此模型"), ("back", "返回")],
            )
            if action == "test":
                self.selected_selector = selector
                self._test_selected()
            elif action == "delete":
                self._delete_model_selector(selector)
            return
        while True:
            self._write("已有模型")
            self._write_model_rows(rows)
            self._write("输入编号测试模型，D 删除模型配置，0 返回。")
            self._write("请选择：", end="")
            choice = self._readline().strip()
            if not choice or choice == "0":
                return
            if choice.casefold() == "d":
                self._delete_model(rows)
                return
            if choice.isdigit() and 1 <= int(choice) <= len(rows):
                self.selected_selector = rows[int(choice) - 1]
                self._write(f"已选择 {self.selected_selector}。")
                self._test_selected()
                continue
            self._write("请选择列表中的编号或 0。")

    def _delete_model(self, rows: list[str]) -> None:
        self._write("删除模型配置")
        self._write_model_rows(rows)
        self._write("请输入要删除的编号，或输入 0 返回：", end="")
        choice = self._readline().strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
            return
        selector = rows[int(choice) - 1]
        self._write(f"删除“{self._model_label(selector)}”及其本地配置？[y/N] ", end="")
        if self._readline().strip().casefold() not in {"y", "yes"}:
            return
        try:
            result = self.models.remove(selector)
            if selector == self.session_selector:
                self.session_selector = None
            if result["connection_removed"]:
                self._write("已删除模型、连接和对应 API Key。")
            else:
                self._write("已删除模型配置。")
        except DomainError as error:
            self._error(error)

    def _delete_model_selector(self, selector: str) -> None:
        choice = self._pick(
            "删除模型",
            f"确定删除“{self._model_label(selector)}”吗？\n最后一个模型还会删除连接和对应 API Key。",
            [("delete", "删除"), ("back", "返回")],
        )
        if choice != "delete":
            return
        try:
            result = self.models.remove(selector)
            if selector == self.session_selector:
                self.session_selector = None
            message = "已删除模型、连接和对应 API Key。" if result["connection_removed"] else "已删除模型配置。"
            self._pick("删除完成", message, [("ok", "返回模型设置")])
        except DomainError as error:
            self._pick("删除失败", f"{error.code}: {error.message}", [("ok", "返回")])

    def _write_model_rows(self, rows: list[str]) -> None:
        for index, selector in enumerate(rows, 1):
            try:
                self.models.resolve(explicit_selector=selector)
                state = "本地可用"
            except DomainError as error:
                state = f"需要修复：{error.code}"
            marker = "*" if selector == self.session_selector else " "
            self._write(f" {index}. {marker} {self._model_label(selector)} · {state}")

    def _model_label(self, selector: str) -> str:
        listing = self._listing()
        if listing is not None:
            for item in cast(list[dict[str, object]], listing["models"]):
                if selector in cast(list[str], item["selectors"]):
                    return str(item["display_name"])
        return selector

    def _choose(self, selector: str) -> None:
        self.selected_selector = selector
        if self._selector_enabled():
            action = self._pick(
                "模型已连接",
                f"{self._model_label(selector)}\n\n选择该模型的使用方式。",
                [("session", "仅当前终端使用"), ("default", "设为全局默认模型"), ("back", "暂不选择")],
            )
            if action == "session":
                try:
                    self.models.resolve(explicit_selector=selector)
                    self.session_selector = selector
                    self._pick("已选择", "该模型仅用于当前终端会话。", [("ok", "返回模型设置")])
                except DomainError as error:
                    self._pick("无法选择模型", f"{error.code}: {error.message}", [("ok", "返回")])
            elif action == "default":
                try:
                    self.models.set_default(selector)
                    self._pick("已保存", "该模型已设为全局默认。", [("ok", "返回模型设置")])
                except DomainError as error:
                    self._pick("无法保存默认模型", f"{error.code}: {error.message}", [("ok", "返回")])
            return
        self._write(f"已选择：{self._model_label(selector)}。")
        self._write("1. 当前终端使用  2. 设为默认模型  0. 返回")
        self._write("请选择操作：", end="")
        action = self._readline().strip()
        if action == "1":
            try:
                self.models.resolve(explicit_selector=selector)
                self.session_selector = selector
                self._write("已保存为会话选择，仅保留在内存中。")
            except DomainError as error:
                self._error(error)
        elif action == "2":
            try:
                self.models.set_default(selector)
                self._write("已保存默认模型。")
            except DomainError as error:
                self._error(error)

    def _test_selected(self) -> None:
        if self.selected_selector is None:
            self._write("请先选择一个模型。")
            return
        try:
            self._write("正在测试连接…")
            status = self.models.test(self.selected_selector, request=True)
            message = f"{self._model_label(self.selected_selector)}\n\n连接成功：{status['status']}。"
            if self._selector_enabled():
                self._pick("连接测试", message, [("ok", "返回")])
            else:
                self._write(f"连接成功：{status['status']}。")
        except DomainError as error:
            if self._selector_enabled():
                self._pick("连接失败", f"{error.code}: {error.message}", [("ok", "返回")])
            else:
                self._error(error)

    def _configure(self) -> None:
        if self._selector_enabled():
            choice = self._pick(
                "配置模型服务商",
                "官方服务商会自动填入兼容地址；自定义服务商需要填写地址。\n\n↑/↓ 选择 · Enter 确定 · Esc 返回",
                [("official", "官方服务商"), ("custom", "自定义 OpenAI-compatible 服务商"), ("back", "返回")],
            )
            if choice in {None, "back"}:
                return
        else:
            self._write("配置模型服务商")
            self._write("1. 官方服务商")
            self._write("2. 自定义 OpenAI-compatible 服务商")
            self._write("0. 返回")
            self._write("请选择：", end="")
            choice = self._readline().strip()
            if choice == "0" or not choice:
                return
            choice = {"1": "official", "2": "custom"}.get(choice, choice)
        if choice == "official":
            provider = self._choose_provider()
        elif choice == "custom":
            name = self._configuration_value("服务商名称")
            if name is None:
                return
            base_url = self._configuration_value("服务地址（OpenAI-compatible Base URL）")
            if base_url is None:
                return
            provider = _ProviderOption("custom", name, base_url, ())
        else:
            self._write("请选择服务商类型。")
            return
        if provider is None:
            return
        self._configure_provider(provider)

    def _configure_provider(self, provider: _ProviderOption) -> None:
        base_url = provider.base_url
        if base_url is None:
            label = "百炼 OpenAI-compatible Base URL（从控制台复制）" if provider.identifier == "bailian" else "服务地址（OpenAI-compatible Base URL）"
            base_url = self._configuration_value(label)
            if base_url is None:
                return
        model_id, display_name = self._choose_provider_model(provider)
        if model_id is None:
            return
        connection_id = self._next_connection_id(provider.identifier)
        reference = SecretStore.file_reference(connection_id)
        key = self._read_secret()
        saved = False
        try:
            connection = ProviderConnection(connection_id=connection_id, base_url=base_url,
                timeout_seconds=60, credential_ref=reference, revision=1)
            model = ModelEntry(connection_id=connection_id, model_id=model_id,
                display_name=f"{provider.name} · {display_name}", output_limit=4096)
            selector = f"{connection_id}/{model_id}"
            self.selected_selector = selector
            self._write("正在测试连接…")
            saved = self._test_draft_configured(connection, model, key)
            if saved:
                self._choose(selector)
        except DomainError as error:
            self._error(error)
            if saved:
                self._write("配置已保留；修复后可在“查看或测试已有模型”中重新验证。")
            else:
                self._write("配置尚未保存；请修复后重新配置。")
        except ValueError:
            self._write("MODEL_CONFIGURATION_INVALID: 模型配置无效")

    def _test_draft_configured(self, connection: ProviderConnection, model: ModelEntry, api_key: str) -> bool:
        while True:
            try:
                self.models.test_draft(connection, model, api_key)
                SecretStore(settings_dir=self.settings_dir).set_file(connection.connection_id, api_key)
                self.models.add(connection, model)
                self._write("连接成功。")
                return True
            except DomainError as error:
                if self._selector_enabled():
                    choice = self._pick(
                        "连接失败",
                        f"{error.code}: {error.message}\n\n当前配置和 API Key 未保存。",
                        [("retry", "重新输入 API Key"), ("back", "返回")],
                    )
                    if choice != "retry":
                        return False
                else:
                    self._error(error)
                    self._write("1. 重新输入 API Key  0. 返回")
                    self._write("请选择：", end="")
                    if self._readline().strip() != "1":
                        return False
                api_key = self._read_secret()

    def _choose_provider(self) -> _ProviderOption | None:
        if self._selector_enabled():
            identifier = self._pick(
                "选择官方服务商",
                "选择后再挑选推荐模型，或手动输入模型 ID。\n\n↑/↓ 选择 · Enter 确定 · Esc 返回",
                [(provider.identifier, provider.name) for provider in _PROVIDERS],
            )
            return next((provider for provider in _PROVIDERS if provider.identifier == identifier), None)
        self._write("选择官方服务商（输入 0 返回）")
        for index, provider in enumerate(_PROVIDERS, 1):
            self._write(f"{index}. {provider.name}")
        self._write("请选择：", end="")
        choice = self._readline().strip()
        if choice == "0" or not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(_PROVIDERS):
            return _PROVIDERS[int(choice) - 1]
        self._write("请选择列表中的编号。")
        return None

    def _choose_provider_model(self, provider: _ProviderOption) -> tuple[str | None, str]:
        if self._selector_enabled():
            choices = [(model_id, display_name) for model_id, display_name in provider.models]
            choices.append(("__custom__", "输入其他模型 ID"))
            choice = self._pick(
                f"选择 {provider.name} 的模型",
                "推荐模型来自本地目录；也可以输入服务商控制台提供的模型 ID。\n\n↑/↓ 选择 · Enter 确定 · Esc 返回",
                choices,
            )
            if choice is None:
                return None, ""
            if choice == "__custom__":
                model_id = self._configuration_value("模型 ID（从服务商控制台或文档复制）")
                return model_id, model_id or ""
            return next((model for model in provider.models if model[0] == choice), (None, ""))
        self._write(f"选择 {provider.name} 的模型")
        for index, (_, name) in enumerate(provider.models, 1):
            self._write(f"{index}. {name}")
        custom = len(provider.models) + 1
        self._write(f"{custom}. 输入其他模型 ID")
        self._write("0. 返回")
        self._write("请选择：", end="")
        choice = self._readline().strip()
        if choice == "0" or not choice:
            return None, ""
        if choice.isdigit() and 1 <= int(choice) <= len(provider.models):
            return provider.models[int(choice) - 1]
        if choice == str(custom):
            model_id = self._configuration_value("模型 ID（从服务商控制台或文档复制）")
            return model_id, model_id or ""
        self._write("请选择列表中的编号；本次配置未保存。")
        return None, ""

    def _next_connection_id(self, prefix: str) -> str:
        listing = self._listing()
        existing = set() if listing is None else {
            str(item["connection_id"])
            for item in cast(list[dict[str, object]], listing["models"])
        }
        index = 1
        while f"{prefix}-{index}" in existing:
            index += 1
        return f"{prefix}-{index}"

    def _configuration_value(self, label: str) -> str | None:
        self._write(f"{label}（输入 :cancel 取消）：", end="")
        value = self._readline().strip()
        if value == ":cancel":
            self._write("已取消本次配置；未保存新的连接或模型。")
            return None
        return value

    def _error(self, error: DomainError) -> None:
        self._write(f"{error.code}: {error.message}")

    def _selector_enabled(self) -> bool:
        import sys

        if self.input is None or self.output is None or sys.stdin is None or sys.stdout is None:
            return False
        return self.input is sys.stdin and self.output is sys.stdout and sys.stdin.isatty() and sys.stdout.isatty()

    def _pick(self, title: str, text: str, choices: list[tuple[str, str]]) -> str | None:
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        def _cancel(event: object) -> None:
            cast(object, event).app.exit(result=None)  # type: ignore[attr-defined]

        return choice(
            message=f"{title}\n\n{text}",
            options=choices,
            style=_SELECTOR_STYLE,
            symbol="›",
            bottom_toolbar="↑/↓ 选择 · Enter 确定 · Esc 返回",
            show_frame=True,
            key_bindings=bindings,
        )

    def _write(self, value: str, *, end: str = "\n") -> None:
        assert self.output is not None
        self.output.write(value + end)
        self.output.flush()

    def _readline(self) -> str:
        assert self.input is not None
        return self.input.readline()

    def _read_secret(self) -> str:
        import getpass
        import sys

        if self._selector_enabled():
            return prompt("API Key（输入已隐藏；按 Enter 后立即测试）：", is_password=True)
        if self.input is sys.stdin and sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass("API Key（输入已隐藏；按 Enter 后立即测试）：")
        return self._readline().rstrip("\r\n")
