# Code Forge · Agent Runtime 架构基线

当前交付是供实现智能体使用的架构、核心代码与接口契约，并增加了一个仅用于本机验证的标准库 Agent Runtime 和最小 Platform。重点是 Agent Runtime；Platform 只验证主流程。

已确定：Deep Agents/LangGraph、自研 Runtime、两个应用、Runtime 内部角色合并部署；Python 在所在机器通过子进程执行，保留沙箱扩展；Skill 手工编辑并按 Run 保存不可变快照；验证阶段默认授权。

## 实现者阅读顺序

1. [需求与已确认决策](requirements.md)
2. [详细设计](docs/detailed-design.md)、[完整流程与分支](docs/agent-workflow.md)、[逐分支测试用例](docs/testing/agent-workflow-cases.md)
3. [Platform 接入约定](docs/api/platform-runtime.md) 与 [OpenAPI](docs/api/openapi.json)
4. [PostgreSQL 表结构](db/migrations/001_runtime.sql) 与 [事务/字段映射](docs/database.md)
5. [核心契约](src/code_forge/contracts.py)、[状态机](src/code_forge/state_machine.py)、[接受流程](src/code_forge/service.py)、[可替换接口](src/code_forge/ports.py)
6. [代码目录与设计规范](docs/coding-standards.md) 和 [实现任务包与验收规则](docs/implementation-guide.md)

## 核心验证

核心代码仅依赖 Python 3.12+ 标准库：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/export_contracts.py
python3 scripts/export_workflow_cases.py
```

测试覆盖状态不变量、幂等接受顺序、契约一致性，以及 SQLite/Skill 快照/本机子进程/HTTP 冒烟链路。测试替身不证明 PostgreSQL 并发、真实模型或 Deep Agents/LangGraph 主流程已经集成成功；DDL 尚未在 PostgreSQL 实例执行。详见 [本轮验证记录](docs/verification.md)。

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

Runtime API 是 `http://127.0.0.1:8000`，页面是 `http://127.0.0.1:8001`。只启动 `run_platform.py` 会显示 Runtime 不可达。页面支持历史 Session、Run 列表、用户/模型消息、流式 `message.delta`、工具/Skill 执行轨迹和 Workspace 文件查看。

插件选择与替换边界见 [Runtime 插件架构](docs/plugin-architecture.md)。可通过 `FORGE_STORE`、`FORGE_EXECUTION`、`FORGE_CONTEXT`、`FORGE_MODEL`、`FORGE_HARNESS` 等环境变量切换实现。

## 当前实现边界

已有核心代码、架构文档、标准库 HTTP/SSE 服务、SQLite 存储、手工 Skill 快照、LocalProcessBackend、确定性本地 Harness、后台 Worker 和最小 Platform。

已有 LangGraph + DeepSeek 真实模型工具循环和本地 SQLite 多轮会话存储。待实现真实 PostgreSQL Repository/migration runner、Deep Agents 可选增强、未知结果核验、多副本恢复与沙箱。Skill 编辑发布、权限后台、审批、文件/报告专用展示仍后置。默认权限与本机子进程仅适用于可信验证，不能宣称生产多租户隔离。

[参考 Agent 能力](docs/agent-capabilities.md) · [评审与决策历史](docs/requirements-review.md)
