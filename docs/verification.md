# 架构核心验证记录

## 2026-10-04：可选 Impala 原生工具

本轮范围为 A 部门共用 LDAP 账号 1 的工具适配器与后续部门映射配置；未配置实际集群地址/凭据，不默认启用或连接示例数据源。

| 验证 | 结果 | 边界 |
| --- | --- | --- |
| 全量单元/适配/契约测试 | 97 项通过，约 7.7 秒 | 新增 30 项 Impala 测试；包括既有 Python/Bash、Session/Message、迁移和契约回归 |
| Impala SQL/身份/额度/取消 | 30 项新测试中覆盖 | SQLGlot 真实 AST；HS2/LDAP/Ranger 行为使用替身；验证拒绝写入/账号参数、分部门映射、结果截断、跨 Message 小时预算、并发、停用、超时、取消与 UNKNOWN 不重放 |
| Impyla API | 固定 0.23.0 的真实 RPC/游标对象离线检查通过 | 单次尝试需 retries=1；arraysize 控制只读 buffersize；没有真实网络认证 |
| 框架/传输/存储 | Local 与真实 LangGraph 工具循环、localhost HTTP/SSE、SQLite 和 User Root 结果/transcript 通过 | 模型和数仓为确定性替身，不能称真实模型自主查询或真实 Impala 连接成功 |
| 生成物与静态检查 | 两个导出脚本、Ruff check/format、git diff --check 通过 | 无新增公开字段、状态、事件或数据库表；测试注册表只增加部分适配证据，原完整流程状态仍 SPEC_ONLY |
| 真实集群与生产安全 | 未验证 | TLS/LDAP/Ranger、真实查询选项/资源池/远端取消、生产认证/沙箱和多副本限流需要另验 |

命令：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`；执行环境为 Python 3.12.0 / macOS arm64。测试日志保存在本机 `/tmp/code-forge-impala-tests.log`，使用随机临时库和无秘密替身。驱动/解析器及传递依赖固定在 `requirements-impala.lock`。接入配置与使用见 [Impala 工具](impala-tools.md)。

## 2026-09-14：既有架构基线

日期：2026-09-14。验证对象为架构核心、本地 Runtime、SQLite schema v4、Platform Session/Message 动作型接口、Session 级 SSE 游标、用户默认/显式多 Skill 路径和最小 Platform。

| 验证 | 结果 | 能力边界 |
| --- | --- | --- |
| 核心、适配与迁移测试 | 66 项通过 | 状态不变量、幂等、UserBinding、Session/Message 原子忙碌拒绝、公开 Message 终止与幂等、用户默认/显式多 Skill 路径、Skill 冻结候选约束、Workspace lease/revision、create-session 路径绑定刷新、子进程 stdin/环境白名单/超时诊断、工具输入与输出分片事件、SQLite v1→v4 迁移 |
| PostgreSQL 语法解析 | pglast 8.4 解析 197 条语句通过 | 没有在 PostgreSQL 实例执行，不证明迁移/FK/真实并发运行通过 |
| JSON Schema | 生成 68 个组件 schema，一致性检查通过 | 不等于所有生产客户端已完成兼容验证 |
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
