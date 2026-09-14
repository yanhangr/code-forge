"""Runtime plugin registry and built-in implementation wiring."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from code_forge.contracts import DomainError, ErrorCode
from code_forge.ports import (
    AgentHarnessPort,
    AuthorizationPort,
    ContextManagerPort,
    ExecutionBackend,
    ModelAdapterPort,
    RunRepository,
    RuntimeStorePort,
    SnapshotResolver,
    WorkspacePort,
)

PluginFactory = Callable[..., Any]


class PluginRegistry:
    """Small explicit registry; external packages can register replacements."""

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, PluginFactory]] = {}

    def register(self, kind: str, name: str, factory: PluginFactory) -> None:
        self._plugins.setdefault(kind, {})[name] = factory

    def create(self, kind: str, name: str, **kwargs: Any) -> Any:
        try:
            factory = self._plugins[kind][name]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"Unknown {kind} plugin: {name}",
            ) from exc
        return factory(**kwargs)

    def names(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted(self._plugins.get(kind, {})))


@dataclass(frozen=True)
class RuntimePlugins:
    store: RuntimeStorePort
    repository: RunRepository
    authorization: AuthorizationPort
    resolver: SnapshotResolver
    workspace: WorkspacePort
    execution: ExecutionBackend
    context_manager: ContextManagerPort
    model_adapter: ModelAdapterPort | None
    harness: AgentHarnessPort


def _sqlite_store(*, root: Path, env: Mapping[str, str], **_: Any) -> Any:
    from code_forge.persistence.sqlite_store import SqliteRuntimeStore

    return SqliteRuntimeStore(root / "state.db")


def _sqlite_repository(*, store: RuntimeStorePort, **_: Any) -> Any:
    from code_forge.persistence.sqlite_store import SqliteRunRepository

    return SqliteRunRepository(store)


def _default_allow_authorization(**_kwargs: Any) -> Any:
    from code_forge.ports import DefaultAllowAuthorization

    return DefaultAllowAuthorization()


def _manual_skills(
    *,
    skills_root: Path,
    snapshot_root: Path,
    **_: Any,
) -> Any:
    from code_forge.skills.manual_skill_resolver import ManualSkillResolver

    return ManualSkillResolver(skills_root, snapshot_root)


def _local_workspace(*, workspace_root: Path, **_: Any) -> Any:
    from code_forge.workspace.store import WorkspaceStore

    return WorkspaceStore(workspace_root)


def _local_process_execution(
    *,
    workspace: WorkspacePort,
    operations_root: Path,
    **_: Any,
) -> Any:
    from code_forge.execution.local_process_backend import LocalProcessBackend

    return LocalProcessBackend(workspace, operations_root)


def _bounded_context(*, max_chars: int, **_: Any) -> Any:
    from code_forge.harness.context import ConversationContextManager

    return ConversationContextManager(max_chars=max_chars)


def _none_model(**_: Any) -> None:
    return None


def _deepseek_model(
    *,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    **_: Any,
) -> Any:
    if not api_key:
        raise DomainError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            "DEEPSEEK_API_KEY is required for the deepseek model plugin",
        )
    from code_forge.integrations.deepseek_chat import DeepSeekChatAdapter

    return DeepSeekChatAdapter(api_key, model=model, base_url=base_url)


def _local_harness(
    *,
    store: RuntimeStorePort,
    execution: ExecutionBackend,
    workspace: WorkspacePort,
    resolver: SnapshotResolver,
    model_adapter: ModelAdapterPort | None,
    max_tool_steps: int,
    **_: Any,
) -> Any:
    from code_forge.harness.local import LocalDeterministicHarness

    return LocalDeterministicHarness(
        store,
        execution,
        workspace,
        worker_id="worker-1",
        model_adapter=model_adapter,
        resolver=resolver,
        max_tool_steps=max_tool_steps,
    )


def _langgraph_harness(
    *,
    store: RuntimeStorePort,
    execution: ExecutionBackend,
    workspace: WorkspacePort,
    resolver: SnapshotResolver,
    context_manager: ContextManagerPort,
    model_adapter: ModelAdapterPort | None,
    max_tool_steps: int,
    **_: Any,
) -> Any:
    if model_adapter is None:
        raise DomainError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            "LangGraph harness requires a configured model adapter",
        )
    try:
        from code_forge.harness.langgraph_harness import LangGraphHarness
    except ImportError as exc:
        raise DomainError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "LangGraph plugin is unavailable. Install it with "
            "`.venv/bin/python -m pip install 'langgraph>=1.2,<2'` "
            "or set FORGE_HARNESS=local.",
        ) from exc

    return LangGraphHarness(
        store,
        execution,
        workspace,
        model_adapter=model_adapter,
        resolver=resolver,
        context_manager=context_manager,
        max_tool_steps=max_tool_steps,
        worker_id="worker-1",
    )


def build_builtin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register("store", "sqlite", _sqlite_store)
    registry.register("repository", "sqlite", _sqlite_repository)
    registry.register("authorization", "default-allow", _default_allow_authorization)
    registry.register("skills", "manual", _manual_skills)
    registry.register("workspace", "local", _local_workspace)
    registry.register("execution", "local-process", _local_process_execution)
    registry.register("context", "bounded", _bounded_context)
    registry.register("model", "none", _none_model)
    registry.register("model", "deepseek", _deepseek_model)
    registry.register("harness", "local", _local_harness)
    registry.register("harness", "langgraph", _langgraph_harness)
    return registry


def build_plugins(
    root: Path,
    env: Mapping[str, str] | None = None,
    *,
    registry: PluginRegistry | None = None,
) -> RuntimePlugins:
    env = env or os.environ
    registry = registry or build_builtin_registry()

    store_name = env.get("FORGE_STORE", "sqlite")
    store = registry.create("store", store_name, root=root, env=env)
    repository = registry.create(
        "repository",
        env.get("FORGE_REPOSITORY", store_name),
        store=store,
        env=env,
    )
    authorization = registry.create(
        "authorization",
        env.get("FORGE_AUTHORIZATION", "default-allow"),
        env=env,
    )
    skills_root = Path(env.get("FORGE_SKILLS_DIR", root / "skills"))
    if not skills_root.is_absolute():
        skills_root = root / skills_root
    resolver = registry.create(
        "skills",
        env.get("FORGE_SKILLS", "manual"),
        skills_root=skills_root,
        snapshot_root=root / "skill-snapshots",
        env=env,
    )
    workspace = registry.create(
        "workspace",
        env.get("FORGE_WORKSPACE", "local"),
        workspace_root=root / "workspace-store",
        env=env,
    )
    execution = registry.create(
        "execution",
        env.get("FORGE_EXECUTION", "local-process"),
        workspace=workspace,
        operations_root=root / "operations",
        env=env,
    )
    context_manager = registry.create(
        "context",
        env.get("FORGE_CONTEXT", "bounded"),
        max_chars=int(env.get("FORGE_CONTEXT_MAX_CHARS", "24000")),
        env=env,
    )

    model_name = env.get("FORGE_MODEL")
    model_adapter = None
    if model_name or env.get("DEEPSEEK_API_KEY"):
        model_adapter = registry.create(
            "model",
            model_name or "deepseek",
            api_key=env.get("DEEPSEEK_API_KEY"),
            model=env.get("DEEPSEEK_MODEL"),
            base_url=env.get("DEEPSEEK_BASE_URL"),
            env=env,
        )

    harness_name = env.get("FORGE_HARNESS")
    if not harness_name:
        harness_name = "langgraph" if model_adapter is not None else "local"
    harness = registry.create(
        "harness",
        harness_name,
        store=store,
        execution=execution,
        workspace=workspace,
        resolver=resolver,
        context_manager=context_manager,
        model_adapter=model_adapter,
        max_tool_steps=int(env.get("FORGE_MAX_TOOL_STEPS", "12")),
        env=env,
    )
    return RuntimePlugins(
        store=store,
        repository=repository,
        authorization=authorization,
        resolver=resolver,
        workspace=workspace,
        execution=execution,
        context_manager=context_manager,
        model_adapter=model_adapter,
        harness=harness,
    )
