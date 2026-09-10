# 通用 Agent 底座：参考能力与适配决策

日期：2026-09-09。目的：学习 OpenCode、Claude Code、Codex 的公开 Agent 能力，将其转化为本项目可实现、可验收的 Harness 契约。已阅读官方文档，未进行源码级对等验证、性能测试或竞品全面评测。

本项目依赖 Deep Agents/LangGraph 库及自研 Runtime，不运行三个参考产品来承载用户任务，也不引入付费 Agent 托管产品。模型供应商保持可配置。本稿中的“采用”表示设计建议，落地后仍需验证。

> 当前范围（2026-09-10）：Runtime 优先，两个应用、内部角色合并部署；Python 本机子进程，沙箱后置。Skill 手工编辑、默认授权，Platform 仅验证主流程。本文是参考能力，不是本轮已实现功能；具体实现任务见 implementation-guide.md。

## 1. 参考事实与本项目选择

| 参考 | 官方资料支持的能力 | 适合借鉴的原因 | 本项目具体采用方式 |
| --- | --- | --- | --- |
| OpenCode Server | Headless HTTP 服务与 OpenAPI，客户端和服务端分离 | 底座可被 Web、内部系统、后续终端复用 | REST + SSE 契约，底座无业务 UI 依赖；不能据此认为已有集群 HA。[官方资料](https://opencode.ai/docs/server/) |
| OpenCode Agents | 主 Agent 与 SubAgent，Build/Plan 等配置 | 工作模式可通过配置组合，不必复制多套引擎 | 一个 Harness，General/Coding/Data AgentSpec；Plan/Review 收窄实际能力。[官方资料](https://opencode.ai/docs/agents/) |
| OpenCode Tools | 文件工具、搜索、命令执行与补丁应用 | Coding 必须具备真实操作和观察反馈 | 底座统一 read/search/apply_patch/exec/Git，结果带版本、状态与证据。[官方资料](https://opencode.ai/docs/tools/) |
| OpenCode Rules | 项目规则和指令文件 | 支持仓库级约定与可维护配置 | 以 AGENTS.md 为默认项目契约；其他指令文件通过显式兼容适配导入。[官方资料](https://opencode.ai/docs/rules/) |
| Claude Code 工作循环 | 收集上下文、执行动作、验证结果循环；工具结果继续影响决策 | 生成代码之后继续构建、测试、修复才是完整 Coding | 复用 Deep Agents 的工具循环，加验证证据与退出条件，禁止只生成一段代码就宣称完成。[官方资料](https://code.claude.com/docs/en/how-claude-code-works) |
| Claude Code Hooks | 生命周期事件上的可配置 Hook | 确定性格式化、检查和外部通知可以不依赖模型记住执行 | 版本化 before_tool/after_tool/before_finalize 等扩展，执行超时、输出有界、不可授予权限。[官方资料](https://code.claude.com/docs/en/hooks-guide) |
| Claude Code SubAgent | 独立上下文和受限工具的子任务 | 主上下文不被大量探索过程占满 | 首版 Explore/Review，结果带来源；父级预算/取消；并行写代理后置。[官方资料](https://code.claude.com/docs/en/sub-agents) |
| Claude Code Checkpointing | 文件变更快照和会话恢复/回溯能力；存在追踪范围限制 | 用户需要知道改了什么、能恢复什么 | 持久 Workspace 修订和 ChangeSet；独立于 LangGraph 检查点，远端副作用不能靠文件回溯撤销。[官方资料](https://code.claude.com/docs/en/checkpointing) |
| Codex AGENTS.md | 启动任务时读取分层项目指令 | 长任务和多人项目需要一致的约定 | InstructionResolver 记录来源、目录范围、内容摘要；可信运行策略优先，仓库内容不能扩权。[官方资料](https://learn.chatgpt.com/docs/agent-configuration/agents-md) |
| Codex App Server | Thread/Turn/Item 组织的交互、状态与增量事件 | 前端不仅需要最终文本，还需任务与工具进展 | Session/Run/MessagePart/ToolExecution 映射为稳定 API；自己的事件版本与持久回放。[官方资料](https://learn.chatgpt.com/docs/app-server) |

上述能力按当前实际读取的文档理解。OpenCode 同时存在不同文档版本路径，字段命名并不直接复制；本项目公共协议独立定义。官方资料介绍某项功能不代表该实现可原样嵌入共享多租户集群。

## 2. 首版 Harness 能力契约

| 能力 | 行为要求 | 验收例子 |
| --- | --- | --- |
| 自主完成任务 | 在预算内持续执行，遇到失败读取证据并调整；简单任务不强制生成繁复计划 | 给一个可复现 bug，修改后运行相关测试，输出验证结果 |
| 计划与进度 | 复杂任务生成结构化 todo，状态随真实执行更新；计划不是执行事实 | 新增接口任务包括实现、测试、检查，未跑测试不得标已验证 |
| 项目指令 | 处理根目录与子目录约定，限定作用范围；冲突/来源可见 | 修改子包时适用该目录 AGENTS.md；不会访问宿主个人指令 |
| 上下文治理 | 文件/日志按需读取，工具结果有界，压缩保留目标、限制、证据和待执行项 | 长测试输出保留错误摘要与完整日志引用，后续可继续定位 |
| 受控工具与脚本 | 所有工具路径统一经过能力检查、执行记录、预算和结果归一化 | 同一文件不能从 read 被拒绝后再经 shell 无约束读取 |
| Skill 激活 | 显式选择优先；自动选择受候选快照限制；活动版本可见 | 100 个注册 Skill，本次仅 3 个候选，不拉取其他 97 个正文 |
| 子任务隔离 | 子任务只收到必要上下文及允许资源，结果具有结构和来源 | 主 Coding Agent 让 Explore 定位入口，子任务不可修改仓库 |
| 验证与收尾 | 区分任务是否完成、测试是否通过、是否受环境阻塞 | 构建工具缺失时返回未验证及原因，不把模型判断当测试通过 |
| 变更可审阅 | 相对任务起点输出多文件 Diff，保留用户原有改动与非 Git 文件 | 新建文件、删除文件、二进制变更都有产物/摘要，不能遗漏 untracked |
| 可继续交互 | 通用消息、控制消息与操作输出分开；断线不丢已保存结果 | 后续要求调整接口参数时沿用前轮代码成果 |

工程映射：`AgentFactory` 组装框架能力；`InstructionResolver` / `ContextManager` / `ToolRouter` / `ExecutionBackend` 承担平台语义；`EventAdapter` 输出稳定事件。不能在 Deep Agents 外再写一个互相竞争的 LLM 工具循环。

## 3. 权限机制如何借鉴而不耦合身份

借鉴的是“执行前检查、必要时等待、明确展示结果”的机制。用户是谁、角色是什么、谁审批、能否访问某张业务表，由上层或数仓网关决定。

例如，上层传入只读仓库和可用分析工具。底座允许搜索代码和查询数据，拒绝写文件；需要额外操作时返回待决请求。上层通过自己的授权服务决定并返回结果。底座只验证决定绑定了同一资源、操作和有效期，不导入上层角色模型。

仅给 Agent 隐藏编辑工具还不够：若同时开放可写 shell，就会绕过“只读”模式。因此文件/进程/网络边界必须在 ExecutionBackend 中一起落地。这个约束是通用执行语义，与业务 RBAC 解耦。

## 4. 有意识后置的能力

- 交互终端、PTY、常驻开发服务器和进程级会话恢复：首版提供非交互命令、日志与取消即可支持开发闭环。
- 并行写入的 Agent 团队：先验证单主写入者和只读子任务，后续加入隔离分支、合并和冲突处理。
- 完整 IDE、LSP 常驻索引：首版利用文件/文本搜索、构建诊断和可选语言工具，不阻塞后续语义搜索适配。
- 自由安装宿主 Plugin 或运行上层 Hook：只接受版本化注册的扩展，脚本在执行环境运行。
- 全量永久 Memory：先做好会话压缩、来源和历史修订；不让长期记忆成为跨作用域数据通道。

## 5. 验证与限制

参考产品的优秀能力不能通过配置名字等价获得。需用真实仓库完成修改—测试—修复，验证长上下文、指令冲突、失败恢复与命令输出；再用数据分析任务验证同一底座可复用。

模型质量、工具契约、上下文构造和执行环境共同决定结果。只证明 Deep Agents 可以运行，不代表已达到这些能力要求。框架兼容门槛与验收步骤见 [详细设计](detailed-design.md)。
