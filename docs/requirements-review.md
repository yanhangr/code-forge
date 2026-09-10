# requirements.md 第一阶段评审记录

日期：2026-09-09。范围：需求完整性、架构边界、生产可行性和首版范围；不含代码实现或真实集群验证。

评审对象为 [原方案](requirements-original.md)，修订结果为 [requirements.md](../requirements.md)。原文件尚未被 Git 跟踪，因此保留了独立原稿供比对，未提交 Git。

> 历史记录说明：下列内容保留第一轮评审依据。用户随后已确认完整 Coding 首版需要、终端后置、采用自研 Runtime，且身份与业务授权在上层。最新边界以主文档为准；本记录的旧建议不再作为待确认事项或实施要求。

## 1. 结论

原方案适合作为技术方向讨论稿，尚不适合作为“最终开工基线”。它在 Agent Harness、状态运行时和企业平台之间划分了合理职责，也识别了 Session/Run、工具副作用、Skill 渐进加载和 Workspace 生命周期等核心概念。

主要缺口是：从框架能力直接跳到了生产保证；对共享算力与独立工作体验缺少验收定义；数仓与 Skill 这两个主要场景还没有形成完整契约。建议保留主路线，先确认首版范围，再补齐可靠性、隔离、版本与数据治理，不需要现在增加大量新平台组件。

## 2. 必须保留的设计

- 通用 Agent 定位；通过配置、Skill 和 Tool 扩展业务能力。
- Product Session 与 Run 分开，产品对象不直接绑定框架 API。
- API/Worker 可横向扩容，不以 Sticky Session 保证正确性。
- 同一主 Thread 的串行写入约束。
- Tool 统一接入，Sandbox 属于一种执行后端。
- Skill 按需读取，同 Run 版本固定；Workspace 不随 Sandbox 销毁。
- 产品事件适配层，首版不引入纯 Event Sourcing。
- Temporal、完整 IDE 和复杂多 Agent 协作按需求后置。

## 3. 问题与修订追踪

P0：会影响首版安全、正确性或核心架构，须在设计冻结前解决。P1：首版生产化需要补齐。P2：后续增强，保留演进边界即可。

| 编号 / 优先级 | 原文定位 | 问题与实际后果 | 修订建议 | 修订章节 |
| --- | --- | --- | --- | --- |
| R01 / P0 | 第四、八节：thread_id 不变即可恢复 | 缺少任务持久接收、租约接管、旧 Worker 写入隔绝、代码兼容和外部状态核验；网络分区时可能双写 | 明确 Run/Attempt、稳定请求 ID、Outbox、租约/fencing 和恢复对账；检查点写入也必须覆盖 | §3、§5、§13 |
| R02 / P0 | 第九节：SUCCEEDED 时直接复用 | 远端成功、本地记录前退出仍会重复；重新推理还可能生成新 tool_call_id | 持久化逻辑操作意图；按幂等、可查询、结果未知分级，不能承诺全局 exactly-once | §6 |
| R03 / P0 | 第十一、二十五节：V1 无 Sandbox，可 Local | 与通用 Agent、Python 分析和脚本 Skill 冲突；共享进程可访问其他租户和平台密钥 | 执行代码就具备隔离；无隔离则明确禁用；完整自研 Manager 可以后置 | §4、§14 |
| R04 / P0 | 第十三、十四节：Skill@版本 | 只写版本号无法固定脚本、依赖、工具 schema；旧指令可能留在会话历史 | 整包不可变、依赖锁定、Run 快照、升级上下文策略、撤销例外、历史包保留 | §7 |
| R05 / P0 | 第六、十八节：统一 Context；User 下挂 Project | Context 是标签，不是授权；共享项目、服务账号与下游数据权限缺失 | Membership 与资源 ACL；服务端身份推导；执行侧数仓授权与隔离验证 | §3、§8、§10 |
| R06 / P0 | 第十、二十三节：SQL 作为普通 Tool 接入 | 接通接口不代表分析可信；无口径、行列权限、扫描预算或来源追溯 | 业务语义、元数据、只读控制、异步查询、大结果引用、报告来源构成首版场景 | §8 |
| R07 / P1 | 第五、二十三节：PG/Redis/K8s 多副本 | 缺少依赖 HA、备份恢复、滚动升级、容量与服务指标；无法承诺高可用 | 应用故障与存储灾难分开定义 RPO/RTO，加入监控、压测和故障矩阵 | §13、§15 |
| R08 / P1 | 第十七节：Redis Stream / PubSub 可互换 | Pub/Sub 不补发离线消息，Stream 也需定义裁剪和持久性；缺少序号和事实来源 | 持久关键事件、Run 内序号、SSE 游标、去重、快照恢复与慢客户端控制 | §12 |
| R09 / P1 | 第十二、二十三、二十七节：存储与 Workspace 后置 | 分析图表/报告和 Skill 脚本已经需要可靠存储；对象存储不能直接等同文件系统 | V1 复用可靠对象/文件存储，区分 Workspace/Artifact，定义提交与恢复语义 | §9 |
| R10 / P1 | 第八节：Run 状态枚举 | 没定义审批与普通消息、取消与实际停止、超时、等待释放资源和重试区别 | 状态行为与单写入不变量一起定义；取消传播并保留外部真实结果 | §5、§6 |
| R11 / P1 | 全文：资源复用只有无状态 Pod | 缺少配额、公平性、预热、回收、资源档位与等待成本；大租户可压垮其他人 | 共享编排池与隔离执行池区分；预算累计、准入与公平调度 | §1、§4 |
| R12 / P1 | 第七、二十六节：Runtime 可替换 | 两条路线长期不选会重复建设；Adapter 不能免除 Checkpoint 迁移；商业部署条件未核实 | 确认商业/网络约束，用相同故障用例选择一种 Runtime | §14 |
| R13 / P1 | 第十、十五节：MCP、SubAgent | 未定义凭据隔离、工具契约变更、子任务预算、取消和文件合并 | 受控连接、稳定契约、有限子任务与父级权限/成本边界 | §8、§11 |
| R14 / P1 | 第二十一节：模型可替换 | 未定义能力兼容、模型出域、token/费用预算、限流与非确定性 | 模型适配职责先内置，受控降级与预算限制；记录版本和限制 | §10 |
| R15 / P1 | 第十六、二十七节：审计与验证 | 没有业务质量、注入测试、版本评测与删除保留规则 | 真实任务评测集、发布回归、安全/故障验收，覆盖数据生命周期 | §9、§12、§15 |
| R16 / P2 | 第十九至二十一节：优于参考产品 | 技术灵活性不等于实际优势，竞品能力随版本变化；不能用对比表替代验收 | 删除未证实优劣结论，以连续体验、正确性、成本和扩展结果作为目标 | §1、§14、§15 |

## 4. 两个需要特别解释的边界

### 热加载不是运行中替换正在执行的代码

用户需要“不预装所有 Skill、可随时发布和选择能力”，可通过版本化注册中心、按 Run 解析、按需激活和分层读取实现。

推荐例子：项目跟随 stable 通道。R100 被接受时解析到分析 Skill v18，只取摘要；执行需要分析时才获取 v18 正文和脚本。发布 v19 后，R100 即使故障接管或等待审批仍继续 v18；R101 才解析到 v19。紧急撤销 v18 时，R100 停止后续相关操作并提示，不偷换 v19。

这是建议的平台行为，不是 Deep Agents 自动提供的完整版本系统。同样，冻结配置提升可追溯性，并不保证模型响应或不断变化的数仓结果完全复现。

### 硬件共享不是所有代码运行在一个无隔离进程里

用户的会话、配置和文件是逻辑资源，可以长期存在；Worker、执行环境和连接按需分配。真正节约来自共享池、并发调度、等待时释放资源、按需启动与空闲回收。

文件连续与进程连续的成本差别很大。首版如果主要做数仓分析，持久文件加可重建的脚本执行通常更容易形成闭环；若要求终端、后台服务和内存变量跨天存在，需要增加环境租约、内核恢复和闲置成本设计。这里需要产品确认，不能从“像单机”自行推定。

## 5. 技术依据与核实范围

2026-09-09 查阅以下官方资料。在线文档会变化；本次确认能力边界，不指定未经验证的框架发行版本。项目当前只有 README 与需求稿，没有实现代码或依赖锁文件，无法核实运行兼容性。

| 资料 | 核实结论与使用限制 |
| --- | --- |
| [Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview) | 可作为 Harness 候选；不能据此推导企业平台已完成 |
| [Skills](https://docs.langchain.com/oss/python/deepagents/skills) | 支持渐进式读取和构造动态 Skill 列表；本平台仍需版本注册、快照和发布治理 |
| [Backends](https://docs.langchain.com/oss/python/deepagents/backends) | LocalShell 在宿主执行，不能把虚拟根目录当作 shell 隔离 |
| [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) / [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) | 提供持久状态机制；分布式运行权及外部执行语义须另外证明 |
| [Subagents](https://docs.langchain.com/oss/python/deepagents/subagents) | 支持编译后的自定义图；仍需验证权限、预算、恢复与结果合并 |
| [Agent Server](https://docs.langchain.com/langsmith/agent-server) / [Self-host standalone servers](https://docs.langchain.com/langsmith/deploy-standalone-server) | 可作为服务运行时；独立部署涉及许可及网络条件，需选型前确认 |
| [MCP Authorization 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) | 授权受众与下游凭据边界的参考；不代表已选定目标协议版本 |

本文的租约、版本快照、首版范围和验收场景是针对本项目的设计建议，尚未通过原型、压测或生产故障演练。所有服务目标均为待确认提案。

## 6. 用户确认结果（2026-09-09）

- 首版：通用对话、完整 Coding、数仓分析、图表/报告；交互终端体验后置。
- 执行：隔离 Python/Skill 脚本及 Coding 命令；文件持久，进程和内存状态不保证长期存活。
- Skill：Run 接受时锁版本，执行中按需激活，新 Run 按上层项目绑定升级。
- Runtime：Deep Agents/LangGraph + 自研集群 Runtime，不使用付费 Agent Server。
- 边界：初期只建设通用底座，身份、组织、业务授权、审批规则在上层。R05 等安全问题保留，但以通用执行约束和上层接口解决，不在底座实现 Membership/RBAC。

后续产物：[详细设计](detailed-design.md)、[参考能力研究](agent-capabilities.md)。容量、首批语言工具链和现有基础设施作为实施配置输入，未冒充已确认或已验证。

## 7. 最新部署调整（覆盖前述一期沙箱建议）

用户要求一期仅两个应用：应用层负责会话展示、Skill 管理、权限管控；Agent 运行应用内部保留模块分层，API/调度/Worker/恢复扫描合并部署，后续按资源瓶颈拆分。每种应用可多副本，数据库等基础设施不算额外自研应用。

沙箱一期不建设，保留 ExecutionBackend，后续 Python 等迁入沙箱。此前一期强制沙箱/隔离执行服务建议不再适用；一期 Python/构建/测试具体执行位置正在向用户确认，未自行默认本机执行或删除完整 Coding 目标。

Skill 发布主数据与通道归应用层，运行应用消费版本解析结果并保存快照/缓存。最新版见 [两应用详设](detailed-design.md)。

## 8. Runtime 优先与架构师交付范围（2026-09-10）

用户确认 Python 一期在 Agent 所在机器以子进程执行，后续接 SandboxBackend。当前 Runtime 是重点，Platform 只验证主流程；Skill 手工编辑，权限默认允许，编辑发布/权限后台/审批/文件和报告展示后置。第 7 节的一期执行位置待确认项已解决，Platform 发布主数据建设不再属于当前任务。

用户进一步明确架构师交付核心功能架构、核心代码、表结构、流程与状态、详细文档；具体适配器和应用实现由其他智能体完成。本轮已把探索中的完整服务代码收敛为 contracts/state_machine/service/ports 核心，避免交付半成品应用。

新规范入口：[需求基线](../requirements.md)、[详细设计](detailed-design.md)、[OpenAPI](api/openapi.json)、[实现交接](implementation-guide.md)。此前独立部署、一期沙箱及完整管理产品等建议均以当前基线为准。

## 9. 流程、实现规范与表结构补充（2026-09-10）

用户要求补齐Agent全流程及分支、Deep Agents/LangGraph各阶段角色和对应测试；增加目录与代码设计规范；PG表/字段备注及date_created/created_by/date_updated/updated_by，索引仅主键/普通/唯一。

现已增加 [完整流程](agent-workflow.md)、[逐分支测试](testing/agent-workflow-cases.md)、[代码规范](coding-standards.md) 与根AGENTS.md。所有自管表完成备注/审计字段，移除条件索引，以活动指针、行锁及CAS代替唯一运行者条件索引。Session/Run接口审计字段同步修订；最新结果见 [验证记录](verification.md)。
