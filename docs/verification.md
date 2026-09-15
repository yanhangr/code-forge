# 架构核心验证记录

日期：2026-09-14。验证对象为架构核心、本地 Runtime、SQLite schema v4、Platform Session/Message 动作型接口、Session 级 SSE 游标、用户默认/显式多 Skill 路径和最小 Platform。

| 验证 | 结果 | 能力边界 |
| --- | --- | --- |
| 核心、适配与迁移测试 | 59 项通过 | 状态不变量、幂等、UserBinding、Session/Message 原子忙碌拒绝、公开 Message 终止与幂等、用户默认/显式多 Skill 路径、Workspace lease/revision、子进程 stdin/环境白名单/超时诊断、工具执行输入/输出读取、SQLite v1→v4 迁移 |
| PostgreSQL 语法解析 | pglast 8.4 解析 197 条语句通过 | 没有在 PostgreSQL 实例执行，不证明迁移/FK/真实并发运行通过 |
| JSON Schema | 生成 70 个组件 schema，一致性检查通过 | 不等于所有生产客户端已完成兼容验证 |
| 接口文档示例 | Session、Message、动作型请求和公开事件通过 schema 验证 | 不代表发起过真实网络请求 |
| 流程分支与测试规格 | B01—B48均有TC-Bxx定义 | 48个完整集成用例均为SPEC_ONLY，未冒充通过 |
| SQL审计/备注/索引结构 | 10表、156字段注释及四个审计字段；6个显式普通/唯一索引，无条件/表达式索引 | 唯一执行权的真实PG并发验证仍待实施 |
| 代码规范 | Ruff 0.16.6 lint和format检查通过 | 架构核心及生成脚本保持统一风格 |
| 文档引用/格式 | 本地链接、代码围栏、空白检查通过 | 历史稿只作存档 |
| 真实模型、Deep Agents 集成 | 本次未执行 | 由实现智能体验证 L2/L3 |
| 子进程适配器、SQLite Runtime、最小 Platform | 已实现并通过本地验证 | 不证明 PostgreSQL、多副本或 VM 沙箱已通过 |
| 浏览器 Session/Message 主流程 | Playwright 实测按用户路径筛选历史、直接编辑会话名称、逐字 SSE 输出、发送 Python Message、显示工具/Skill 轨迹和 Workspace 文件；测试同时断言用户 Session 目录、JSONL transcript 和 tool-output；桌面与 390px 移动视口截图无重叠 | 仅证明 trusted logical 模式，不证明用户容器/执行沙箱；首次默认 Runtime 端口探测和 favicon 仍有无害 404 |
| 多副本 HA、数仓与沙箱 | 未验证 | 分别作为后续专项验收 |

核心复验命令：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/export_contracts.py
python3 scripts/export_workflow_cases.py
```

核心代码只依赖标准库。pglast/jsonschema/Ruff 是本次额外验证工具，不是 Runtime 生产依赖。实施者按 [交接说明](implementation-guide.md) 补齐真实适配器与分层测试，不能使用本报告替代集成验收。
