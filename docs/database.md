# 数据库实现约束与事务边界

日期：2026-09-10。DDL：[001_runtime.sql](../db/migrations/001_runtime.sql)。目标数据库 PostgreSQL；当前仅完成语法解析，未执行真实迁移或并发测试。迁移是一次性版本迁移，不能反复裸执行 CREATE TYPE；实现者提供 migration runner 和测试数据库。

## 1. 对象关系

```mermaid
erDiagram
    WORKSPACES ||--o{ WORKSPACE_REVISIONS : commits
    WORKSPACES ||--o| SESSIONS : binds
    SESSIONS ||--o{ RUNS : queues
    SESSIONS ||--o{ SESSION_REQUESTS : deduplicates
    RUNS ||--o{ RUN_ATTEMPTS : executes
    RUNS ||--o{ TOOL_EXECUTIONS : invokes
    RUNS ||--o{ PENDING_RESPONSES : waits
    RUNS ||--o{ RUN_EVENTS : emits
    RUNS ||--o{ ARTIFACTS : produces
```

scope_id 为所有运行对象的隔离域。Runtime 不创建用户、角色、项目和 Skill 发布表。手工 Skill 的确定包引用放在 runs.config_snapshot；未来 Registry 属于 Platform，Runtime 仍只持久快照。

## 2. API 与表字段映射

| API/核心字段 | 数据位置 | 说明 |
| --- | --- | --- |
| Run.id/session_id/run_seq | runs 对应列 | UUID；run_seq 在 Session 内正序 |
| RunCreate.context | runs.execution_context | 规范化 scope/actor/external 引用；无真实凭据 |
| date_created/created_by/date_updated/updated_by | 所有自管表对应列；Session/Run读接口同名字段 | 由服务生成，不接受客户端自由写入 |
| Run.snapshot / RunSnapshot | runs.config_snapshot | 不可变对象，字段与核心 DTO/OpenAPI 完全一致 |
| AcceptedRun.reused | 响应时计算 | 不存数据库；判断命中已有请求 |
| status/state_version/task_outcome | runs 对应列 | 使用核心枚举与状态机 |
| wait_reason | runs.wait_reason | 仅等待状态非空，离开等待时清空 |
| Event.event_id/run_id/seq/type/occurred_at/data | run_events 对应列 | schema_version 固定为字符串 "1" |
| SSE id / event_cursor | 从 run_id 和 seq 计算 | 格式 run_id:seq，不是独立数据库 ID |
| Session.workspace_id | sessions.workspace_id | 不由客户端直接填任意本机路径 |
| OperationSpec.operation_id | tool_executions.id | 与实际 PID/external_operation_ref 分离 |
| OperationSpec argv/资源/profile | tool_executions.input_ref 指向的不可变输入对象及 profile 列 | 参数摘要参与幂等；不可把不同命令绑定同一操作 ID |
| PendingResponse.prompt | pending_responses.payload.prompt | payload 结构由 kind 的处理器校验 |
| PendingResponse.resolved | resolved_at 非空 | 不额外保存第二个布尔事实 |

查询层可以投影字段，但不能建立另一套可独立写的运行状态。config_snapshot、request_fingerprint、input、run_seq 在接受后不可变；禁止通用 ORM patch API 修改这些字段。SDK 序列化 JSON 时必须保存所有必需快照字段，不能用字符串 `repr(dataclass)`。

## 3. 统一锁顺序

需要多个运行对象的事务按 Session → Run → Attempt → Tool/Pending 的顺序加锁，短事务内完成，不持锁调用模型、执行命令或下载 Skill。创建 Session 涉及新 Workspace 与 session_requests 时使用单独的创建事务。

选择任务时可先查询候选 Session，再用 SKIP LOCKED 领取 Session，随后验证其队列和 active_run；不要先锁 Run 再反向等 Session 导致与接受/取消路径死锁。实现者可以优化 SQL，但必须保持统一锁顺序与正确性。

## 4. 五条原子边界

### 接受 Run

校验 Session scope、锁 Session、重新查 idempotency_key。存在则比较 fingerprint：相同读取原 Run 返回，不分配序号；不同报冲突。不存在则从 next_run_seq 分配顺序、插入含 config_snapshot 的 QUEUED Run、写第一条接受事件、更新 Session 序号，一次提交。

只有事务提交之后响应 202。外部快照字节先保存，事务失败产生的无引用内容可延后清理。已保存的幂等记录不能因为任务完成立刻删除，否则网络重试可能重复任务。

### 领取执行

锁 Session 后检查 active_run：无主任务时只允许队首；已有主任务则只能恢复同一 Run。增加 execution_epoch、设置/验证 active_run_id、写 Attempt 与 lease、更新 Run.active_attempt_id，转 RUNNING 并写 run.started，一次提交。

本版移除条件索引。唯一主任务/尝试由 sessions.active_run_id、runs.active_attempt_id、统一行锁、epoch和CAS一起保证。先结束旧Attempt，再建立新Attempt并更新活动指针，同一事务提交；不能仅查询 status 或 ended_at 就无锁认定执行权。

### 状态与事件

加载 RunState，调用核心 transition；UPDATE 使用预期 state_version，活跃 Worker 写还需当前 Attempt/epoch/有效 lease。0 行更新是 STATE_CONFLICT，不当成功。changed=False 的取消不写第二条取消事件。

事件序号通过锁住 Run 后递增 next_event_seq 分配。状态更新与关键事件同事务；DELTA 可按批次合并，但发布的事件必须可回放。不能先通过 SSE 发布一个尚未持久、却承诺一定能回放的关键事实。

### 工具意图与结果

图的工具计划先持久化，再以稳定 logical_call_key 建立 PREPARED 操作。重复槽同摘要查回，不同摘要报冲突。派发之后外部执行与数据库不在同一事务，返回丢失时查外部句柄/本机操作记录；不能把 timeout 自动等价于没执行。

结果文件/日志先持久，再提交 result_ref 和状态/事件。process_ref 仅供核验，不能只凭 PID 自动认领或杀进程。UNKNOWN 是有意义的状态，不应被通用失败重试覆盖。

### 完成或消费回应

完成：验证输出与成果引用、更新 Run 终态和结果、结束 Attempt、清理 Run.active_attempt_id 和 Session.active_run_id、写 run.finished，在事务中完成。

回应：先验证归属，再查 response_key 的既有消费记录；同摘要重试返回当前 Run，不因原 expected_state_version 已过期报错。新回应才验证 pending 未解决及预期版本，原子保存回应、恢复 QUEUED 和 run.resumed。审批类型仍是预留，不要求一期实现审批工作流。

## 5. 检查点与工作区的独立事实

框架 Checkpointer 自己管理其序列化表。必须验证 put 与 put_writes 的租约检查和写入共用事务连接；外层先查租约再调用独立 saver 不能保证防旧写入。

Workspace 的 manifest/内容先可靠写入，数据库 current_revision 再用预期父修订 CAS 更新。代码/文件物理写入与图检查点不原子，工具账本需保存输入/输出修订，恢复先核验再继续。部分失败修改可保留，但新 Run 应知道前序失败，不自动恢复旧文件或清除用户修改。

如果一个失败/取消的图仍有 pending tool calls，下一轮不能直接 invoke 旧 checkpoint 触发它们。实现者需验证非执行式收尾；若框架无法安全规范化，则为 Session 创建新 thread_id，携带已确认的会话事实和 Workspace，保留旧 Attempt/checkpoint 供审计。该映射调整不改变产品 session_id，且不得把未确认工具结果编造为成功。

## 6. 审计字段与索引规范

所有10张自管表使用 date_created、created_by、date_updated、updated_by，包括只追加的事件/修订表和幂等请求表；每张表/字段有 COMMENT ON。date_created/date_updated 类型为 timestamptz，不是仅保存日期的 date。

- 新增：两组时间和主体相同；可信 actor_ref 表示用户动作，后台用 system:runtime/api、system:runtime/worker 或 system:runtime/reconciler。created_by/updated_by 不设默认值，适配器必须显式传入，禁止空白或伪造用户身份。
- 修改：保留新增字段，更新 date_updated/updated_by。核心 audit.on_update 提供纯规则，时间由适配器在获得锁后读取数据库时钟，例如 SELECT clock_timestamp()，避免长事务开始时间早于上一笔提交。
- 幂等复用/无修改：四个字段不变。事件/不可变修订正常只插入，date_updated保持初始值；运维修复需独立审计，不任意改写历史。
- 时间默认值只处理 INSERT，不会自动刷新 UPDATE；Repository 每条有实际修改的 SQL 必须写修改审计字段。客户端不可在请求中指定这四个字段；Session/Run读接口按同名输出。
- occurred_at、lease_until、heartbeat_at、due_at、deadline_at、ended_at、resolved_at 等是业务时间，保留各自语义，不能以 date_updated 代替。

索引只允许主键、简单列普通 B-tree、简单列唯一索引；可复合列。移除了所有条件索引，不使用表达式、INCLUDE、GIN/GiST等特殊形式。外键/CHECK/enum仍保留，它们是完整性约束而不是新索引类别。

索引目的：idx_runs_status_due_created 扫描队列，idx_runs_session_status_seq 查Session队列，idx_attempts_ended_lease 扫描需核验尝试，idx_attempts_run_ended 查Run尝试。UNIQUE约束负责幂等键、序号及作用域引用的唯一性；活动执行权由指针事务保证。

## 7. 迁移与验证门槛

最低真实数据库验证：创建/回滚迁移、同键并发接受、Session 顺序、等待主任务阻塞、取消/完成竞态、旧 epoch 写拒绝、跨 scope 外键拒绝、事件 seq 唯一、snapshot 查询水位一致。使用真实 PostgreSQL，不用内存 FakeRepository 代替这些验收。

DDL 的字符串/JSON 之外还需适配器做来源、JSON schema、文件归属和语义校验。代码里的纯状态机不承担数据库事务；数据库约束也不证明进程/外部副作用已被隔离。
