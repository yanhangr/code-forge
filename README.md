# Code Forge · Agent Runtime 架构基线

当前交付是供实现智能体使用的架构、核心代码与接口契约。重点是 Agent Runtime；Platform 只验证主流程。尚未交付运行中的 Runtime/Platform。

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

测试覆盖状态不变量、幂等接受顺序和契约一致性。测试替身不证明 PostgreSQL 并发、真实模型或主流程已经集成成功；DDL 尚未在 PostgreSQL 实例执行。详见 [本轮验证记录](docs/verification.md)。

## 当前实现边界

已有核心代码与架构文档。待实现 HTTP/SSE 服务、存储/Skill/LocalProcessBackend 适配器、Deep Agents 集成、调度恢复及最小 Platform。

Skill 编辑发布、权限后台、审批、文件/报告专用展示和沙箱后置。默认权限与本机子进程仅适用于可信验证，不能宣称生产多租户隔离。

[参考 Agent 能力](docs/agent-capabilities.md) · [评审与决策历史](docs/requirements-review.md)
