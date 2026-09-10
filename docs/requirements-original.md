下面这份可以作为我们正式开工前的**最终架构基线**。后续如果没有新的硬约束，我建议就围绕这套方案继续做详细设计，不再频繁换核心技术路线。

# 一、最终目标

我们不是单纯复刻 OpenCode，也不是做一个只服务数据分析的 Agent。

最终目标是：

> **建设一个通用、多用户、可扩展的企业 Agent Platform，支持 Coding、数据分析、文档处理、业务工具调用、动态 Skill、SubAgent，并能逐步演进到生产级 Sandbox 和长任务调度。**

核心技术路线确定为：

```text
Deep Agents
    ↓
LangChain Agent
    ↓
LangGraph
```

其中：

```text
Deep Agents
= Agent Harness

LangGraph
= Agent Runtime / State Runtime

自研 Platform
= 企业产品层
```

---

# 二、最终总体架构

```text
┌──────────────────────────────────────────────┐
│                    Web                       │
│                                              │
│ Chat / Skill Editor / Project / Files        │
│ Agent配置 / 管理台 / 审批 / 运行状态          │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│               Agent Platform                 │
│                                              │
│ Tenant / User / Project                      │
│ Session / Run                                │
│ Agent Registry                               │
│ Skill Registry                               │
│ Tool Registry                                │
│ Permission / RBAC                            │
│ Event Adapter                                │
│ Agent Runtime Adapter                        │
│                                              │
│         ┌────────────────────────┐           │
│         │      Deep Agents       │           │
│         │                        │           │
│         │ Skill                  │           │
│         │ SubAgent               │           │
│         │ Planning               │           │
│         │ Context                │           │
│         │ Backend                │           │
│         │ Middleware             │           │
│         └───────────┬────────────┘           │
│                     ▼                        │
│                 LangGraph                    │
│                                              │
│ Thread / State / Checkpoint                  │
│ Interrupt / Resume / Streaming               │
└─────────────────────┬────────────────────────┘
                      │
                  Tool Layer
                      │
       ┌──────────────┼───────────────┐
       ▼              ▼               ▼
      MCP          Remote Tool     Execution
                     Service        Backend
                       │               │
                 Hive/Impala        Python
                 HTTP API           Bash
                 Email              Git
                 业务系统             Files
                                    ...
```

底层基础设施：

```text
PostgreSQL
Redis
MinIO（需要 Artifact / Workspace 后）
Kubernetes
```

---

# 三、最重要的职责边界

以后架构设计都围绕这句话：

> **Platform 管“谁在做什么”，Deep Agents 管“Agent 怎么做”，LangGraph 管“Agent 做到哪里了”，Tool/Backend 管“真正在哪里执行”。**

Agent Platform 负责：

```text
User
Tenant
Project
Session
Run
Agent配置
Skill版本
Tool权限
RBAC
审计
事件
```

Deep Agents 负责：

```text
Planning
Todo
Skill调用
SubAgent
Context管理
Filesystem abstraction
Backend
Middleware
```

LangGraph 负责：

```text
Thread
Messages state
Checkpoint
Graph state
Interrupt
Resume
Fault recovery
Streaming
```

外部 Tool 负责真正能力：

```text
SQL
MCP
HTTP
Email
Python
Bash
Git
文件
业务服务
```

---

# 四、Session / Thread / Run 最终模型

这是整个架构最核心的数据模型。

```text
Tenant
  ↓
User
  ↓
Project
  ↓
Session
     │
     └── LangGraph Thread
             │
             ├── Run 1
             ├── Run 2
             ├── Run 3
             └── ...
```

## Product Session

这是用户看到的会话。

例如：

```text
“消费金融调价分析”
“某系统代码排查”
“风险指标分析”
```

Session 保存：

```text
session_id
tenant_id
user_id
project_id
title
agent_id
workspace_id
thread_id
created_at
updated_at
```

---

## LangGraph Thread

负责 Agent 内部状态。

例如：

```text
messages
todo
DeepAgentState
loaded state
checkpoint
interrupt
context
```

核心是：

```text
thread_id
```

只要 thread_id 不变，请求落在哪个 Pod 都可以恢复。

---

## Run

一次用户输入触发一次 Run。

例如：

```text
Session S1

用户：
“分析六月数据”
→ Run R1

用户：
“按风险等级再拆”
→ Run R2

用户：
“生成图”
→ Run R3
```

因此：

```text
Session
≠
Run
```

一个 Session 会有很多 Run。

---

# 五、多用户 + 无状态 Agent 微服务怎么实现

Agent Platform Pod 必须真正无状态。

```text
             K8s Service
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
    Pod A      Pod B      Pod C
```

用户 A：

```text
请求1 → Pod A
请求2 → Pod C
请求3 → Pod B
```

完全没问题。

因为 Pod 不保存：

```text
Session
Thread
Checkpoint
Run
Workspace
Skill版本
```

真正状态都在：

```text
PostgreSQL
LangGraph Checkpointer
Redis
MinIO
```

所以：

> **不依赖 Sticky Session。**

Sticky Session 可以优化缓存，但不能成为正确性基础。

---

# 六、多用户数据隔离

同一个 Worker 同时运行多个用户是正常设计。

真正隔离依赖统一：

```text
TenantContext
```

每一个 Run 都带：

```text
tenant_id
user_id
project_id
session_id
thread_id
run_id
workspace_id
```

然后所有：

```text
DB
Cache
Skill
Memory
Artifact
Workspace
Tool
Sandbox
```

都基于这个 Context 做 namespace 和 ACL。

不要有：

```python
current_user
current_session
current_workspace
```

这种全局状态。

---

# 七、Run Runtime 怎么处理

这里我们不直接绑死实现。

定义统一：

```text
AgentRuntime
```

接口类似：

```text
create_thread()
create_run()
stream_run()
resume_run()
cancel_run()
get_state()
get_history()
```

底层有两个候选实现：

```text
A. LangGraph Agent Server

B. 自研
   FastAPI
   PostgreSQL
   Redis
   Run Manager
   Worker
```

LangGraph Agent Server 的 Thread / Run / Queue / Worker / Streaming 架构非常值得借鉴。

但产品层必须通过：

```text
AgentRuntime Adapter
```

隔离。

这样未来替换 Runtime 不影响：

```text
Session
Skill
Tool
Web
RBAC
```

---

# 八、Run 最重要的可靠性要求

无论最终是否使用 LangGraph Agent Server，都必须满足：

```text
同一个 Thread
同时只能一个主 Run 写状态
```

例如：

```text
Thread T1

R1 RUNNING
R2 QUEUED
R3 QUEUED
```

避免两个 Run 同时修改同一个 LangGraph State。

同时 Run 至少需要：

```text
QUEUED
RUNNING
WAITING_USER
SUCCEEDED
FAILED
CANCELLED
RECOVERING
```

Worker 挂掉：

```text
checkpoint还在
↓
新Worker接管
↓
根据thread_id恢复
```

一句话：

> **LangGraph 负责“从哪里继续”，Run Runtime 负责“谁来继续”。**

---

# 九、Tool 副作用必须单独治理

LangGraph checkpoint 不等于 exactly-once。

例如：

```text
send_email
执行成功
↓
Worker挂了
↓
checkpoint没写完
↓
恢复以后再次执行
```

可能发送两遍。

所以 Tool 执行必须有：

```text
tool_call_id
idempotency_key
status
result
```

类似：

```text
tool_execution

tool_call_id
run_id
tool_name
status
result
```

恢复时：

```text
如果已经SUCCEEDED
↓
直接返回历史结果
```

而不是重新执行。

---

# 十、Tool 是整个通用平台的核心扩展机制

Data Gateway 不再是平台一级特殊组件。

我们统一为：

```text
Tool Registry
+
Tool Executor / Router
```

Agent 只知道：

```text
sql_query()
python()
send_email()
read_file()
browser()
```

不知道真正在哪里执行。

内部：

```text
                    Tool Router
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
     PLATFORM          REMOTE          SANDBOX
        │                │                │
      Skill           SQL/API          Python
      Memory          MCP              Bash
      Email           Hive             Git
                      Impala           Files
```

因此新增工具：

```text
Jira
Oracle
Spark
内部系统
```

只需要注册 Tool，不修改 Agent Runtime。

---

# 十一、Sandbox 最终定位

V1 不需要 Sandbox。

Sandbox 本质上是：

> **Tool 的一种 Execution Backend。**

演进路线：

```text
V1
ExecutionBackend = Disabled / Local

V2
ExecutionBackend = Remote Executor

V3
ExecutionBackend = K8s Sandbox
```

以后：

```text
Agent
 ↓
python tool
 ↓
Tool Router
 ↓
SandboxBackend
 ↓
Sandbox Manager
 ↓
Kubernetes
 ↓
Sandbox Pod
```

K8s 负责：

```text
创建
调度
扩缩容
CPU/Memory限制
销毁
```

Sandbox Manager 负责：

```text
Session与Sandbox绑定
生命周期
idle timeout
workspace mount
```

gVisor / Kata 才负责更强的隔离。

---

# 十二、Workspace 必须独立于 Sandbox

以后一定坚持：

```text
Session      长期
Workspace    长期
Sandbox      临时
```

例如：

```text
Session S1
  ↓
Workspace W1
```

今天：

```text
Sandbox SB1
mount W1
↓
运行
↓
销毁
```

明天：

```text
Sandbox SB9
mount W1
↓
继续
```

Session 不应该因为 Sandbox 销毁而丢失工作成果。

---

# 十三、Skill 最终设计

Skill 不只是文件。

最终做：

```text
Skill Registry
```

支持：

```text
System Skill
Team Skill
Project Skill
User Skill
```

并版本化：

```text
Skill A
├── v17
├── v18
└── v19
```

发布状态：

```text
DRAFT
↓
TEST
↓
PUBLISHED
↓
DEPRECATED
```

加载方式继续采用 OpenCode / Deep Agents 的 progressive loading：

```text
先给LLM：
name
description

↓
需要时

load_skill()

↓
加载完整SKILL.md
```

不要一次把所有 Skill 塞入 Context。

---

# 十四、动态 Skill 更新原则

同一个 Run 内：

```text
Skill版本固定
```

Run 之间：

```text
允许动态更新
```

例如：

```text
Run 100
skill@18

此时发布 skill@19

Run 100继续用18
Run 101开始用19
```

否则运行结果不可复现、不可审计。

---

# 十五、SubAgent 最终设计

借鉴 Deep Agents + OpenCode + Claude Code：

```text
Main Agent
   │
   ├── General Agent
   ├── Explore Agent
   ├── Data Agent
   └── Context Collector
           ↓
       Custom LangGraph
```

Deep Agents 一个重要优势：

> SubAgent 可以直接挂一个自定义 LangGraph。

所以复杂、确定性的子流程不用全部交给 LLM 自由发挥。

例如：

```text
Context Collector

R1
↓
R2
↓
R3
↓
R4
↓
R5
↓
R6
```

可以直接是固定 Graph。

普通 SubAgent 默认：

```text
不能继续 spawn SubAgent
```

防止无限递归。

---

# 十六、Event 模型必须做

我们不直接把 LangGraph 内部事件暴露给 Web。

统一：

```text
LangGraph / Deep Agents Event
            ↓
       Event Adapter
            ↓
      Platform Event
```

定义稳定事件：

```text
RUN_STARTED

MESSAGE_CREATED
MESSAGE_DELTA
MESSAGE_COMPLETED

TOOL_STARTED
TOOL_COMPLETED
TOOL_FAILED

SKILL_LOADED

PERMISSION_REQUESTED
PERMISSION_RESOLVED

SUBAGENT_STARTED
SUBAGENT_COMPLETED

COMPACTION_STARTED
COMPACTION_COMPLETED

RUN_FAILED
RUN_COMPLETED
```

这些事件同时服务：

```text
UI
Streaming
Audit
Metrics
Hooks
Debug
```

但不做纯 Event Sourcing。

仍然：

```text
Session表
Run表
Message表
ToolExecution表
+
Event Log
```

---

# 十七、Streaming 最终模型

微服务以后：

```text
Worker
 ↓
Redis Stream / PubSub
 ↓
API Pod
 ↓
SSE
 ↓
Web
```

因此 Worker 与前端连接哪个 API Pod 无关。

用户刷新页面：

```text
重新连接
↓
根据run_id恢复Stream
```

长期状态还是 PostgreSQL。

Redis 只负责：

```text
实时通信
queue
stream
cancel signal
lock/cache
```

---

# 十八、Project 是一级对象

不能只有：

```text
User → Session
```

正确应该：

```text
Tenant
 ↓
User
 ↓
Project
 ↓
Session
```

Project 持有：

```text
默认Agent
Project Skill
Workspace
Tool配置
权限
MCP
业务环境
```

Session 只是这个 Project 中的一次持续 conversation。

---

# 十九、和其他 Agent 的对比结论

这是我们设计过程中最重要的参考。

| 参考系统                       | 我们重点借鉴什么                                                           |
| -------------------------- | ------------------------------------------------------------------ |
| **Deep Agents**            | Harness、Skill、SubAgent、Context、Backend、Middleware、自定义Graph         |
| **LangGraph**              | Thread、Checkpoint、Interrupt、Resume、State                           |
| **LangGraph Agent Server** | Thread/Run、Queue、Worker、Streaming、横向扩容                             |
| **OpenCode**               | Client/Server、Session API、Message Parts、Skill按需加载、Primary/SubAgent |
| **Claude Code**            | Hook生命周期、SubAgent隔离、Permission + Sandbox、成熟Coding交互思想              |
| **Claude Managed Agents**  | Session/Thread/Event-first、多Agent服务器模型                             |
| **Temporal**               | 长任务、跨天Workflow、可靠调度、真正durable workflow                             |

---

# 二十、相比 OpenCode，我们更灵活在哪里

OpenCode 更偏：

```text
Coding-first
```

我们是：

```text
General Agent Platform
```

因此我们可以：

```text
Agent A
Coding

Agent B
数据分析

Agent C
风险分析

Agent D
知识处理

Agent E
Context Collector
```

不同 Agent：

```text
不同模型
不同Skill
不同Tool
不同SubAgent
不同Graph
不同权限
```

底层统一 Deep Agents + LangGraph。

这就是比 OpenCode 更灵活的地方。

---

# 二十一、相比 Claude Code，我们的优势和短板

优势：

```text
模型可替换
Backend可替换
Agent可自定义
SubAgent可以是LangGraph
Skill可以在线管理
企业权限可以深度定制
可与内部系统深度整合
```

短板：

```text
Coding产品成熟度
Terminal体验
Diff体验
LSP
Git UX
Hook成熟度
Permission细节
Context调优
```

这些 Claude Code 已经经过很长时间产品打磨。

所以：

> **架构灵活性我们可以更强，产品成熟度前期一定弱于 Claude Code。**

---

# 二十二、Temporal 最终结论

现在不上。

因为：

```text
学习成本高
运维成本高
基础设施复杂
```

而 LangGraph 已经能解决：

```text
Agent State
Checkpoint
Resume
Interrupt
Session continuity
```

Temporal 以后只在真正出现：

```text
跨天任务
大量定时任务
等待Webhook
跨系统Workflow
精确长期恢复
复杂Retry
```

时再引入。

甚至未来可以：

```text
Temporal
 ↓
LangGraph
```

而不会推翻 Agent Platform。

---

# 二十三、V1 真正部署什么

第一版我建议：

```text
① Web

② Agent Platform

③ 外部 Tool Services
```

其中外部 Tool Services 不是固定服务数量，只是：

```text
MCP
SQL服务
业务API
Email
...
```

按需接。

基础设施：

```text
PostgreSQL
Redis
Kubernetes
```

MinIO：

```text
开始有文件/Artifact/Workspace以后再上
```

---

# 二十四、Agent Platform 内部逻辑结构

即使 V1 一个服务，也建议代码结构逻辑分开：

```text
Agent Platform
│
├── Control Plane
│   ├── Tenant
│   ├── User
│   ├── Project
│   ├── Session
│   ├── Agent Registry
│   ├── Skill Registry
│   ├── Tool Registry
│   ├── Permission
│   └── API
│
└── Runtime Plane
    ├── Deep Agents
    ├── LangGraph
    ├── Agent Runtime
    ├── Worker
    ├── Tool Router
    ├── Event Adapter
    └── Streaming
```

部署时可以：

```text
agent-api Deployment

agent-worker Deployment
```

同镜像，不同启动命令。

以后独立扩容 Worker。

---

# 二十五、现在明确不做

V1 先不要碰：

```text
Temporal
Dynamic Sandbox Manager
Kata
gVisor
完整IDE
复杂LSP
复杂Plugin
高级Memory
多Region
复杂Scheduler
```

防止项目一开始就过度设计。

---

# 二十六、但是这些接口现在必须抽象

后续详细设计一定提前定义：

```text
AgentRuntime

ToolExecutor

ExecutionBackend

SkillBackend

WorkspaceBackend

ArtifactStore

PolicyEngine

EventPublisher
```

这样未来：

```text
Local
→ Remote
→ Sandbox
→ Temporal
```

不会改 Agent 主链。

---

# 二十七、开工顺序

我建议正式进入详设和编码以后，就按这一条主线：

```text
资源模型
Tenant/User/Project
        ↓
Session
        ↓
Thread
        ↓
Run
        ↓
Message
        ↓
ToolCall
        ↓
Event
```

然后实现：

```text
Deep Agents
↓
LangGraph persistence
↓
AgentRuntime
↓
Streaming
↓
Dynamic Skill
↓
Tool Registry
↓
Permission
↓
外部Tools
```

Workspace / Sandbox 后置。

---

# 最后把整个方案压缩成一句话

> **以 Deep Agents 作为 Agent Harness、LangGraph 作为状态与持久化 Runtime，自研多用户 Product Session / Project / Skill / Tool / Permission / Event 平台层；Agent Platform 完全无状态并通过 PostgreSQL + Redis 支撑横向扩容；所有外部能力统一抽象成 Tool，Sandbox只是未来的一种 Execution Backend；OpenCode 提供 Coding 产品架构参考，Claude Code 提供 Hook/SubAgent/Sandbox 参考，Claude Managed Agents 提供 Session/Thread/Event 模型参考，Temporal 暂不引入，仅作为未来长生命周期 Workflow 的演进方向。**

这套可以直接作为**正式开工的架构基线**。
