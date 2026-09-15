# Code Forge · Agent Runtime 架构基线

当前交付是供实现智能体使用的架构、核心代码与接口契约，并增加了一个仅用于本机验证的标准库 Agent Runtime 和最小 Platform。重点是 Agent Runtime；Platform 只验证主流程。

已确定：Deep Agents/LangGraph、自研 Runtime、两个应用、Runtime 内部角色合并部署；Python 在所在机器通过子进程执行，保留沙箱扩展；Skill 手工编辑并按 Run 保存不可变快照；验证阶段默认授权。

## 实现者阅读顺序

1. [需求与已确认决策](requirements.md)
2. [详细设计](docs/detailed-design.md)、[完整流程与分支](docs/agent-workflow.md)、[逐分支测试用例](docs/testing/agent-workflow-cases.md)
3. [Platform ↔ Agent Runtime 接口规范](docs/api/platform-runtime.md) 与 [OpenAPI](docs/api/openapi.json)
4. [PostgreSQL 表结构](db/migrations/001_runtime.sql) 与 [事务/字段映射](docs/database.md)
5. [Agent 流程与数据落点（As-Is）](docs/agent-data-lifecycle.md)
6. [核心契约](src/code_forge/contracts.py)、[状态机](src/code_forge/state_machine.py)、[接受流程](src/code_forge/service.py)、[可替换接口](src/code_forge/ports.py)
7. [代码目录与设计规范](docs/coding-standards.md) 和 [实现任务包与验收规则](docs/implementation-guide.md)

## 核心验证

核心代码仅依赖 Python 3.12+ 标准库：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/export_contracts.py
python3 scripts/export_workflow_cases.py
```

测试覆盖状态不变量、幂等接受顺序、契约一致性、SQLite v1→v4 迁移、平台 Session/Message 接口、Session 级事件游标、用户默认/显式多 Skill 路径、Skill 快照和本机子进程链路。测试替身不证明 PostgreSQL 并发、真实模型或 Deep Agents/LangGraph 主流程已经集成成功；DDL 尚未在 PostgreSQL 实例执行。详见 [本轮验证记录](docs/verification.md)。

## 本地验证入口

首次准备 `.venv`：

```sh
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e '.[langgraph]'
source .venv/bin/activate
```

配置模型和运行参数：

```sh
cp .env.example .env
```

`.env` 示例：

```env
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_VERIFY_SSL=0
FORGE_MAX_TOOL_STEPS=24
FORGE_CONTEXT_MAX_CHARS=24000
```

`scripts/run_local.py` 和 `scripts/run_runtime.py` 会自动读取项目根目录的 `.env`；已存在的系统环境变量优先级更高。`DEEPSEEK_VERIFY_SSL=0` 只用于本机代理改写证书链的验证环境，生产环境应保持证书校验。

同时启动 Runtime 和 Platform：

```sh
python3 scripts/run_local.py --runtime-port 8000 --platform-port 8001
```

或分开启动：

```sh
python3 scripts/run_runtime.py --port 8000
python3 scripts/run_platform.py --port 8001
```

Runtime API 是 `http://127.0.0.1:8000`，页面是 `http://127.0.0.1:8001`。只启动 `run_platform.py` 会显示 Runtime 不可达。页面在 Message 流中内联展示工具/Skill 执行轨迹，包括实际执行的代码、stdout/stderr 和状态；同时支持历史 Session、Message 历史、流式 `message.delta`、终止执行、澄清回复和 Workspace 文件查看。

插件选择与替换边界见 [Runtime 插件架构](docs/plugin-architecture.md)。可通过 `FORGE_STORE`、`FORGE_EXECUTION`、`FORGE_CONTEXT`、`FORGE_MODEL`、`FORGE_HARNESS` 等环境变量切换实现。

## 当前实现边界

已有核心代码、架构文档、标准库 HTTP/SSE 服务、SQLite schema v4、手工 Skill 快照、LocalProcessBackend、确定性本地 Harness、后台 Worker 和最小 Platform。Platform 只使用 Session/Message 动作型接口；Runtime 以用户 `config/skills` 为默认 Skill 根、按 Message 接受显式多路径覆盖，并以项目路径作为 Agent 固定执行根，在 Workspace 级 lease 内发布文件修订。绑定模式下同时维护 `sessions/<session_id>/session.json`、JSONL transcript 和 `tool-output/<session_id>/<run_id>/`。工具实际代码随 `tool.prepared`、输出分片随 `tool.output` 事件实时推送，Platform 无需轮询工具明细接口。公开事件使用 Session 级序号，不暴露内部 Run ID。

已有 LangGraph + DeepSeek 真实模型工具循环和本地 SQLite 多轮会话存储。待实现真实 PostgreSQL Repository/migration runner、Deep Agents 可选增强、未知结果核验、多副本恢复与跨平台沙箱。Skill 编辑发布、权限后台、审批、文件/报告专用展示仍后置。默认权限与本机子进程仅适用于可信验证，不能宣称生产多租户隔离。

[参考 Agent 能力](docs/agent-capabilities.md) · [评审与决策历史](docs/requirements-review.md)
