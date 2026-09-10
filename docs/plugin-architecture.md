# Runtime 插件与替换边界

日期：2026-09-10。Runtime 组合根通过显式插件注册表装配实现；核心 contracts/state_machine/service/ports/audit 不导入具体插件。

## 插件种类

| 插件类型 | 端口 | 内置实现 | 环境变量 |
| --- | --- | --- | --- |
| store | `RuntimeStorePort` | SQLite | `FORGE_STORE=sqlite` |
| repository | `RunRepository` | SQLite Repository | `FORGE_REPOSITORY=sqlite` |
| authorization | `AuthorizationPort` | DefaultAllow | `FORGE_AUTHORIZATION=default-allow` |
| skills | `SnapshotResolver` | ManualSkillResolver | `FORGE_SKILLS=manual` |
| workspace | `WorkspacePort` | Local Workspace | `FORGE_WORKSPACE=local` |
| execution | `ExecutionBackend` | LocalProcessBackend | `FORGE_EXECUTION=local-process` |
| context | `ContextManagerPort` | Bounded ConversationContext | `FORGE_CONTEXT=bounded` |
| model | `ModelAdapterPort` | DeepSeek 或 none | `FORGE_MODEL=deepseek|none` |
| harness | `AgentHarnessPort` | LangGraph 或 Local | `FORGE_HARNESS=langgraph|local` |

## 组合规则

1. `build_plugins()` 根据环境变量创建插件，生成不可变 `RuntimePlugins`。
2. `AgentRuntime` 只接收端口对象，不自行创建数据库、模型或 Harness。
3. Transport 只调用 `RuntimeStorePort` 和 Runtime 服务，不读取插件私有状态。
4. 插件未配置或被关闭时返回 `MODEL_NOT_CONFIGURED` / `DEPENDENCY_UNAVAILABLE`，不静默降级成假成功。
5. 外部实现可通过 `PluginRegistry.register(kind, name, factory)` 注册，再传给 `create_server(..., registry=...)`。

## 示例：替换上下文管理器

```python
from code_forge.runtime.plugins import build_builtin_registry

registry = build_builtin_registry()
registry.register("context", "my-context", lambda **_: MyContextManager())
```

然后设置：

```sh
FORGE_CONTEXT=my-context
```

替换上下文管理器不会改变 Session、Run、事件、SQLite/PostgreSQL 或 Platform HTTP/SSE 契约。
