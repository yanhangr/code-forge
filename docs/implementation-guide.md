# 实现智能体交接与架构验收

日期：2026-09-10。该文件是实施任务说明，不是已完成状态。所有实现者先阅读 README、requirements.md、detailed-design.md、OpenAPI 与本文；不把 history/ 和旧评审建议作为当前开发要求。

实施前补充必读：[完整流程与框架职责](agent-workflow.md)、[B01—B48测试用例](testing/agent-workflow-cases.md)、[代码目录与设计规范](coding-standards.md)。每项实现回交对应TC-Bxx编号。自管表用 date_created/created_by/date_updated/updated_by，所有字段有备注，仅使用简单主键/普通/唯一索引。

## 1. 当前已交付与尚未实现

已交付：无外部依赖的领域枚举/DTO、纯状态机、RunService 接受流程参考实现、可替换端口、默认授权适配器、PostgreSQL DDL、OpenAPI/事件 schema、核心测试与手工 Skill 示例。

尚未实现：HTTP 服务、数据库 Repository/migration runner、真实 Deep Agents/模型适配、LocalProcessBackend、文件/Skill 快照存储、调度与恢复、SSE 分发、最小 Platform、真实数仓连接。不要把 Protocol 方法声明、DDL 文件或测试替身报告为可运行产品。

当前不要求：Skill 编辑发布产品、角色/权限后台、审批工作流、报告/文件专用展示、沙箱、独立 Worker 部署或服务网格。

## 2. 必须遵守的边界

1. 两个应用，Agent Runtime 优先，内部 API/Worker/调度/扫描合并部署。
2. Deep Agents/LangGraph 库 + 自研运行层，不引入付费 Agent 托管依赖，也不以竞品 CLI 代替 Runtime。
3. Python 在所在机器子进程运行，后端实现 ExecutionBackend，不能在 Worker 内直接 eval/exec。
4. Skill 手工编辑，接受 Run 时保存整个包的不可变快照，运行中按需加载；版本不是只记录一个字符串。
5. 当前 DefaultAllowAuthorization；保留接入端口，不建设业务 IAM，不把默认授权描述为生产安全。
6. 接受后任务不能只存在内存；Run 接受、幂等与事件是一个存储事务边界。
7. 同 Session 主写入者唯一；消息重试、故障恢复与用户重新执行是不同动作。
8. 未运行的测试、未知工具效果、未验证的 HA/沙箱必须真实标注，不能用假结果补齐。
9. Platform 只消费契约，不绕过接口读 Runtime 数据库或工作目录。
10. 变更公共字段、状态、事件、表结构或核心行为时先报告架构差异；不在各自代码里另立隐式约定。

## 3. 实现任务包

| 任务包 | 主要工作 | 可独立交付物与验收 |
| --- | --- | --- |
| R1 存储与接受 | 执行 DDL、实现 RunRepository、Session 幂等、CAS、Run 事件原子写入 | 真实 PostgreSQL 上并发同键只产生一个 Run；不同输入冲突；崩溃后已接受记录存在 |
| R2 执行与文件 | LocalProcessBackend、稳定操作 ID、进程组超时/取消、日志、Workspace 修订、手工 Skill 快照 | 真 Python 生成文件；大输出/异常/子进程/取消可核验；改源 Skill 不影响旧快照 |
| R3 Harness | 框架版本锁、AgentFactory、模型配置、工具路由、上下文、结果与事件映射 | 真实模型自主选工具，完成计算/修改/测试；状态与工具结果不是脚本伪造 |
| R4 调度与恢复 | 持久队列、Session 顺序、Attempt/epoch、检查点写入保护、等待/取消/对账 | 重复领取无重复合法写入；重启不盲重放未知本机命令；多轮文件/上下文连续 |
| R5 传输与集成 | 实现 OpenAPI、SSE/快照/分页/错误、接入默认上下文 | 契约验证通过；两个 API 连接可重连回放，完整文本不重复追加 |
| P1 最小 Platform | 会话、输入、Skill 选择、消息/工具输出、取消、刷新重连、澄清输入 | 仅调用 HTTP/SSE；真实主流程可观察；没有产品管理功能前也能验证 Runtime |

任务包可由其他智能体按依赖开发。R1/R2 的接口已定义；R3 集成它们；R4/R5 完成运行链路；P1 可先用严格符合 OpenAPI 的 fixture 对接，但最终必须跑真实 Runtime。是否并行、由几个智能体实现由用户安排，本轮未派发。

建议目录：transport、runtime、persistence、harness、execution、workspace、skills、integrations；Platform 另置 apps/platform。领域 contracts/state_machine/service/ports 不导入这些适配器。具体文件数量和内部辅助类由实现者决定，无需架构师规定每行代码。

## 4. 验证阶梯：不能跨级声称完成

| 级别 | 证明什么 | 不证明什么 |
| --- | --- | --- |
| L0 核心单元测试 | 状态规则、请求摘要、接受顺序与测试替身竞态 | PostgreSQL、框架或模型真实行为 |
| L1 适配集成 | 真数据库、真子进程、真文件/快照、接口 schema | 模型自主推理或业务任务质量 |
| L2 框架链路 | Deep Agents/LangGraph 真正调用工具、持久多轮、受控测试模型 | 真实模型选择能力，不能只称 Agent 已成功 |
| L3 真实模型主流程 | 模型自主完成 Python/文件/Coding 任务，最小页面观察全过程 | 多副本 HA、生产安全、真实数仓未测试部分 |
| L4 故障与部署 | 多副本租约/fencing、网络/进程故障、备份恢复、容量 | 尚未接入的沙箱隔离 |

可以使用确定性测试模型，但测试报告必须标明其用途，不能在用户不知道时把它作为真实 Agent 回复。模型地址/名称/密钥由实现阶段配置，未提供凭据时 L3 保持未验证；不索取用户在文档或聊天中写密钥。

## 5. 核心验收场景

- 提交一个任务，收到 202 后重启服务，任务记录和版本快照仍可读取。
- 同请求重复/并发提交，只有一个 Run；同键改内容返回冲突。
- 手工修改 Skill，旧 Run 的正文、脚本与 digest 不变，新 Run 使用新快照。
- 同会话连续两轮，第二轮看到前轮文件和上下文；运行期间新输入按顺序排队。
- 真 Python 输出计算结果并写文件；退出码、日志、文件与模型回复一致。
- Coding 修改多文件、运行测试并根据失败修复；测试证据绑定实际代码修订。
- 超时/取消能处理子孙进程；服务中断时操作未知不得盲重跑。
- 浏览器断线/刷新后事件去重、补发、完整文本恢复；断线不取消任务。
- 等待澄清时响应原 Run；重复回应不消费两次；默认授权不产生无意义审批。
- 真实外部工具/数仓接入需单独验证；mock 返回正确不算真实连接成功。

## 6. 每个任务包的回交格式

每次实现提交说明：完成范围、修改的模块/契约版本、运行命令、测试级别与结果、实际模型/框架/数据库版本、已知限制、未验证项、相对架构的偏差。证据避免保存凭据和敏感原始数据。

遇到不可行架构点时提供：原约束 → 实际失败证据 → 最小替代方案 → 对 API/状态/持久性/其他任务包的影响。架构更新后同步代码、SQL、生成文件与文档再继续；不绕过约束只让页面看似成功。

本轮的核心测试命令：`PYTHONPATH=src python3 -m unittest discover -s tests -v`。重新生成接口：`PYTHONPATH=src python3 scripts/export_contracts.py`。目前没有启动 Runtime/Platform 的命令，因为应用适配器还未实现。
