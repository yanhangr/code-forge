# Deep Agents 引入必要性

日期：2026-09-10。当前结论：**LangGraph 是 Agent Runtime 的图执行层；Deep Agents 作为 Harness 参考实现和可选增强层，不作为当前主线替代品。**

## 1. 定位

| 层次 | 当前实现 | 职责 |
| --- | --- | --- |
| Agent Runtime | LangGraphHarness + 自研调度/存储/执行 | 图状态、模型/工具循环、Run/Attempt、事件和恢复边界 |
| Agent Harness 参考 | Deep Agents 能力清单 | 规划、文件、Skill、上下文、子任务和中间件设计参考 |
| 可选增强 | 按需采用 | 例如上下文管理、文件系统抽象；不合适时自研 |

Deep Agents 是“Agent 怎么工作”的参考，不能替代 Runtime 的持久队列、CAS、执行账本、事件回放和外部副作用核验。LangGraph 负责“图怎么执行”，自研 Runtime 继续负责“一次任务怎么可靠运行”。

## 2. 何时引入 Deep Agents

满足以下至少一项，并且能通过适配层隔离时再引入：

- 长上下文压缩和来源追踪优于当前实现；
- 文件工具/子任务抽象显著减少自研成本；
- middleware 能统一 before_tool/after_tool 等观测逻辑；
- 仍不把 DA 类型暴露到公开 API、持久状态或 Platform。

## 3. 何时自研

出现以下情况时保留自研：

- 引入后需要绕过 Run/Attempt/Checkpointer 契约；
- 公共状态、事件或数据库事务语义发生变化；
- 只为了“框架名字一致”但无法证明真实执行能力；
- 恢复、取消或外部副作用语义无法满足 B38—B40。

## 4. 当前决定

当前已使用 LangGraph 验证真实 DeepSeek 模型工具循环。Deep Agents 暂不强制引入；优先在本项目自己的上下文、工具和 Skill 编排上完成验证，再判断是否需要 Deep Agents 的上下文管理层。

## 5. 上下文管理决策

当前采用自研 `ConversationContextManager`：

- 从同一 Session 的历史 Run 中按顺序读取用户输入和最终输出；
- 在预算内保留完整消息；
- 超过 `FORGE_CONTEXT_MAX_CHARS` 时保留最近消息，并把更早内容压缩为带角色标签的确定性摘要；
- 不依赖 Deep Agents 的私有状态类型，仍使用 Runtime 可审计的 Run/事件事实。

Deep Agents 暂不作为上下文压缩的必选实现。后续只有在以下条件同时满足时才接入：

1. 压缩结果明显优于当前策略；
2. 能保留目标、约束、证据、待办和 Skill 版本来源；
3. 能通过 adapter 隔离，不改变 Runtime 的存储、恢复和公开事件契约。

届时实现方式是新增 `DeepAgentsContextAdapter`，保持 `ContextManager` 调用边界不变，而不是让 LangGraph 或 Deep Agents 类型进入核心领域。
