# 架构核心验证记录

日期：2026-09-10。验证对象为架构核心与契约，不是 Runtime/Platform 完整应用。

| 验证 | 结果 | 能力边界 |
| --- | --- | --- |
| 核心与契约单元测试 | 29 项通过 | 状态不变量、幂等接受顺序、测试替身并发、代码/生成物/DDL 枚举与字段一致性 |
| PostgreSQL 语法解析 | pglast 8.4 解析 169 条语句通过 | 没有在 PostgreSQL 实例执行，不证明迁移/FK/真实并发运行通过 |
| JSON Schema | jsonschema 4.26.0 校验 46 个组件 schema 通过 | 不等于 HTTP 服务已实现 |
| 接口文档示例 | RunCreate、AcceptedRun、SSE Event 通过 schema 验证 | 不代表发起过真实网络请求 |
| 流程分支与测试规格 | B01—B48均有TC-Bxx定义 | 48个完整集成用例均为SPEC_ONLY，未冒充通过 |
| SQL审计/备注/索引结构 | 10表、132字段全部有备注及四个审计字段；4个显式普通索引，无条件/表达式索引 | 唯一执行权的真实PG并发验证仍待实施 |
| 代码规范 | Ruff 0.16.6 lint和format检查通过 | 架构核心及生成脚本保持统一风格 |
| 文档引用/格式 | 本地链接、代码围栏、空白检查通过 | 历史稿只作存档 |
| 真实模型、Deep Agents 集成 | 未执行 | 由实现智能体验证 L2/L3 |
| 子进程适配器、真实数据库、最小 Platform | 未实现 | 本轮交付 Protocol/DDL/契约，不是应用 |
| 多副本 HA、数仓与沙箱 | 未验证 | 分别作为后续专项验收 |

核心复验命令：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/export_contracts.py
python3 scripts/export_workflow_cases.py
```

核心代码只依赖标准库。pglast/jsonschema/Ruff 是本次额外验证工具，不是 Runtime 生产依赖。实施者按 [交接说明](implementation-guide.md) 补齐真实适配器与分层测试，不能使用本报告替代集成验收。
