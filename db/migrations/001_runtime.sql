-- Runtime 架构基线 DDL，尚未部署；应用适配器负责每次 INSERT/UPDATE 的审计字段。
-- 索引仅使用主键、普通 B-tree 与唯一索引；不使用条件/表达式/覆盖/GIN/GiST 索引。
-- Platform user/role/Skill publishing tables intentionally do not exist here.
BEGIN;
CREATE SCHEMA IF NOT EXISTS runtime;

CREATE TYPE runtime.run_status AS ENUM (
 'QUEUED','RUNNING','WAITING_USER','WAITING_EXTERNAL','RECOVERING','CANCELLING',
 'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT');
CREATE TYPE runtime.task_outcome AS ENUM ('completed','partial','blocked');
CREATE TYPE runtime.tool_status AS ENUM ('PREPARED','RUNNING','SUCCEEDED','FAILED','CANCELLED','UNKNOWN');

CREATE TABLE runtime.workspaces (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL DEFAULT 'default',
 storage_ref text NOT NULL,
 current_revision uuid,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,id)
);
CREATE TABLE runtime.workspace_revisions (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL,
 workspace_id uuid NOT NULL,
 parent_id uuid,
 manifest_digest text NOT NULL,
 manifest_ref text NOT NULL,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,workspace_id,id),
 FOREIGN KEY(scope_id,workspace_id) REFERENCES runtime.workspaces(scope_id,id),
 FOREIGN KEY(scope_id,workspace_id,parent_id) REFERENCES runtime.workspace_revisions(scope_id,workspace_id,id)
);
ALTER TABLE runtime.workspaces ADD CONSTRAINT workspace_current_revision_fk
 FOREIGN KEY(scope_id,id,current_revision) REFERENCES runtime.workspace_revisions(scope_id,workspace_id,id);

CREATE TABLE runtime.sessions (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL DEFAULT 'default',
 workspace_id uuid NOT NULL,
 title text NOT NULL DEFAULT 'New session',
 external_ref text,
 thread_id text NOT NULL UNIQUE,
 next_run_seq bigint NOT NULL DEFAULT 1 CHECK(next_run_seq > 0),
 execution_epoch bigint NOT NULL DEFAULT 0 CHECK(execution_epoch >= 0),
 active_run_id uuid,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,id),
 UNIQUE(scope_id,workspace_id),
 FOREIGN KEY(scope_id,workspace_id) REFERENCES runtime.workspaces(scope_id,id)
);

CREATE TABLE runtime.session_requests (
 scope_id text NOT NULL,
 idempotency_key varchar(200) NOT NULL CHECK(length(idempotency_key)>0),
 request_fingerprint char(64) NOT NULL,
 session_id uuid NOT NULL,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 PRIMARY KEY(scope_id,idempotency_key),
 FOREIGN KEY(scope_id,session_id) REFERENCES runtime.sessions(scope_id,id)
);

CREATE TABLE runtime.runs (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL DEFAULT 'default',
 session_id uuid NOT NULL,
 run_seq bigint NOT NULL CHECK(run_seq > 0),
 idempotency_key varchar(200) NOT NULL CHECK(length(idempotency_key)>0),
 request_fingerprint char(64) NOT NULL,
 input text NOT NULL CHECK(length(input) BETWEEN 1 AND 100000),
 agent_ref text NOT NULL,
 execution_context jsonb NOT NULL,
 config_snapshot jsonb NOT NULL,
 status runtime.run_status NOT NULL DEFAULT 'QUEUED',
 active_attempt_id uuid,
 state_version bigint NOT NULL DEFAULT 0 CHECK(state_version >= 0),
 task_outcome runtime.task_outcome,
 wait_reason text,
 output text,
 error jsonb,
 cancel_requested_at timestamptz,
 deadline_at timestamptz,
 due_at timestamptz NOT NULL DEFAULT now(),
 next_event_seq bigint NOT NULL DEFAULT 1 CHECK(next_event_seq>0),
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,id),
 UNIQUE(scope_id,session_id,id),
 UNIQUE(scope_id,session_id,run_seq),
 UNIQUE(scope_id,session_id,idempotency_key),
 FOREIGN KEY(scope_id,session_id) REFERENCES runtime.sessions(scope_id,id),
 CHECK((status='SUCCEEDED') = (task_outcome IS NOT NULL)),
 CHECK((status IN ('WAITING_USER','WAITING_EXTERNAL')) = (wait_reason IS NOT NULL))
);
CREATE INDEX idx_runs_status_due_created ON runtime.runs(status,due_at,date_created);
CREATE INDEX idx_runs_session_status_seq ON runtime.runs(scope_id,session_id,status,run_seq);
ALTER TABLE runtime.sessions ADD CONSTRAINT session_active_run_fk
 FOREIGN KEY(scope_id,id,active_run_id) REFERENCES runtime.runs(scope_id,session_id,id);

CREATE TABLE runtime.run_attempts (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL,
 session_id uuid NOT NULL,
 run_id uuid NOT NULL,
 epoch bigint NOT NULL CHECK(epoch>0),
 worker_id text NOT NULL,
 lease_until timestamptz NOT NULL,
 heartbeat_at timestamptz NOT NULL,
 workspace_base_revision uuid,
 checkpoint_ref text,
 ended_at timestamptz,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,session_id,epoch),
 UNIQUE(scope_id,run_id,id),
 FOREIGN KEY(scope_id,session_id,run_id) REFERENCES runtime.runs(scope_id,session_id,id)
 -- Adapter validates workspace_base_revision belongs to Session workspace.
);
CREATE INDEX idx_attempts_ended_lease ON runtime.run_attempts(ended_at,lease_until);
CREATE INDEX idx_attempts_run_ended ON runtime.run_attempts(scope_id,run_id,ended_at);
ALTER TABLE runtime.runs ADD CONSTRAINT run_active_attempt_fk
 FOREIGN KEY(scope_id,id,active_attempt_id) REFERENCES runtime.run_attempts(scope_id,run_id,id);

CREATE TABLE runtime.tool_executions (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL,
 run_id uuid NOT NULL,
 logical_call_key text NOT NULL,
 tool_ref text NOT NULL,
 params_digest char(64) NOT NULL,
 input_ref text NOT NULL,
 status runtime.tool_status NOT NULL DEFAULT 'PREPARED',
 execution_profile_ref text NOT NULL,
 external_operation_ref text,
 process_ref jsonb,
 result_ref text,
 error jsonb,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,run_id,id),
 UNIQUE(scope_id,run_id,logical_call_key),
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id)
);
CREATE TABLE runtime.pending_responses (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL,
 run_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('clarification','execution_reconciliation','approval')),
 payload jsonb NOT NULL,
 response_key text,
 response_digest char(64),
 response_payload jsonb,
 resolved_at timestamptz,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,run_id,id),
 UNIQUE(scope_id,run_id,response_key),
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id)
);
-- Approval is a reserved protocol kind, not a requirement to build an approval product.
CREATE TABLE runtime.run_events (
 event_id uuid PRIMARY KEY,
 scope_id text NOT NULL,
 run_id uuid NOT NULL,
 seq bigint NOT NULL CHECK(seq>0),
 schema_version text NOT NULL DEFAULT '1' CHECK(schema_version='1'),
 type text NOT NULL,
 data jsonb NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT now(),
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,run_id,seq),
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id)
);
CREATE TABLE runtime.artifacts (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL,
 run_id uuid NOT NULL,
 storage_ref text NOT NULL,
 media_type text NOT NULL,
 digest text NOT NULL,
 provenance jsonb NOT NULL DEFAULT '{}',
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id)
);
-- LangGraph checkpoint tables are managed by its pinned saver migration.
-- Require a scoped/fenced adapter; do not serialize custom graph state into runs.output.

-- 数据字典：所有自管表及字段必须有 COMMENT。
COMMENT ON TABLE runtime.workspaces IS '持久工作区；current_revision 指向已可靠提交的文件修订。';
COMMENT ON COLUMN runtime.workspaces.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.workspaces.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.workspaces.storage_ref IS '存储适配器解析的文件/对象引用；不是任意客户端宿主路径。';
COMMENT ON COLUMN runtime.workspaces.current_revision IS '已提交修订指针；按预期父修订 CAS 更新。';
COMMENT ON COLUMN runtime.workspaces.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.workspaces.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.workspace_revisions IS '不可变文件修订链；内容先可靠保存，再提交 manifest 引用。';
COMMENT ON COLUMN runtime.workspace_revisions.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.workspace_revisions.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.workspace_revisions.workspace_id IS '所属工作区 UUID；必须与 scope_id 匹配。';
COMMENT ON COLUMN runtime.workspace_revisions.parent_id IS '父修订 UUID；首修订为空，不允许关联其他工作区。';
COMMENT ON COLUMN runtime.workspace_revisions.manifest_digest IS '文件清单的内容摘要，用于完整性校验。';
COMMENT ON COLUMN runtime.workspace_revisions.manifest_ref IS '不可变文件清单的存储引用，提交前确保其内容可读取。';
COMMENT ON COLUMN runtime.workspace_revisions.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.workspace_revisions.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.sessions IS '连续会话与调度门；锁本行管理主 Run 和 Session 单调执行代号。';
COMMENT ON COLUMN runtime.sessions.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.sessions.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.sessions.workspace_id IS '所属工作区 UUID；必须与 scope_id 匹配。';
COMMENT ON COLUMN runtime.sessions.title IS '会话展示标题，不参与推理或授权决策。';
COMMENT ON COLUMN runtime.sessions.external_ref IS '上层业务的可选关联标识，不作为授权凭据。';
COMMENT ON COLUMN runtime.sessions.thread_id IS '当前 LangGraph Thread 标识；产品 Session ID 不随恢复换代改变。';
COMMENT ON COLUMN runtime.sessions.next_run_seq IS '下次接受 Run 使用的正整数序号；在 Session 行锁事务中分配。';
COMMENT ON COLUMN runtime.sessions.execution_epoch IS 'Session 单调递增执行代号；领取/接管时增加，旧写入必须拒绝。';
COMMENT ON COLUMN runtime.sessions.active_run_id IS 'Session 当前主任务指针，等待时保留；终态收尾时清空。';
COMMENT ON COLUMN runtime.sessions.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.sessions.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.session_requests IS '创建会话的幂等请求；同作用域同键只能关联一个确定请求和会话。';
COMMENT ON COLUMN runtime.session_requests.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.session_requests.session_id IS '所属 Session UUID，必须属于相同 scope。';
COMMENT ON COLUMN runtime.session_requests.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.session_requests.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.runs IS '一次被接受的 Agent 任务；配置快照不可变，状态和事件在同一事务提交。';
COMMENT ON COLUMN runtime.runs.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.runs.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.runs.session_id IS '所属 Session UUID，必须属于相同 scope。';
COMMENT ON COLUMN runtime.runs.run_seq IS 'Session 内 Run 接受顺序；网络重试不重复分配。';
COMMENT ON COLUMN runtime.runs.input IS '已接受的用户输入原文，保留代码空白；接受后不可变。';
COMMENT ON COLUMN runtime.runs.agent_ref IS '提交时选择的 Agent 配置引用，例如 general@1。';
COMMENT ON COLUMN runtime.runs.execution_context IS '作用域及调用主体引用；不得保存真实密钥，结构按公开契约校验。';
COMMENT ON COLUMN runtime.runs.config_snapshot IS '不可变 RunSnapshot：Agent/Skill/工具/模型/执行 profile 的确定引用。';
COMMENT ON COLUMN runtime.runs.status IS '当前生命周期状态；通过领域状态机与数据库事务修改。';
COMMENT ON COLUMN runtime.runs.active_attempt_id IS 'Run 唯一有效执行尝试指针；锁 Session/Run 并验证 epoch 后更新，不能仅按 ended_at 查询执行权。';
COMMENT ON COLUMN runtime.runs.state_version IS '状态乐观并发版本；每次合法状态更新递增，0 行 CAS 表示冲突。';
COMMENT ON COLUMN runtime.runs.task_outcome IS '正常协议结束时的 completed/partial/blocked；不等同测试或业务验收通过。';
COMMENT ON COLUMN runtime.runs.wait_reason IS 'WAITING_USER/WAITING_EXTERNAL 的等待原因，其他状态必须为空。';
COMMENT ON COLUMN runtime.runs.output IS '最终回复文本，不代替框架检查点和工具执行事实。';
COMMENT ON COLUMN runtime.runs.error IS '结构化错误，字段遵循契约；不返回堆栈、令牌或敏感环境。';
COMMENT ON COLUMN runtime.runs.cancel_requested_at IS '业务时间：第一次收到有效取消请求的时间，幂等重试不覆盖。';
COMMENT ON COLUMN runtime.runs.deadline_at IS '业务时间：Run 总期限，可空；超时清理和未知操作核验按流程执行。';
COMMENT ON COLUMN runtime.runs.due_at IS '业务时间：下一次可调度时间，用于排队或退避唤醒。';
COMMENT ON COLUMN runtime.runs.next_event_seq IS '下一事件序号；在锁定 Run 的事务中分配，已提交序号不能复用。';
COMMENT ON COLUMN runtime.runs.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.runs.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.run_attempts IS 'Run 的执行尝试/接管记录；唯一有效尝试由 runs.active_attempt_id 与 epoch 校验确定。';
COMMENT ON COLUMN runtime.run_attempts.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.run_attempts.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.run_attempts.session_id IS '所属 Session UUID，必须属于相同 scope。';
COMMENT ON COLUMN runtime.run_attempts.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.run_attempts.epoch IS '该尝试领取时的 Session execution_epoch，失效后不得提交状态/检查点。';
COMMENT ON COLUMN runtime.run_attempts.worker_id IS '具体应用实例/执行者标识；不同进程不能共用同一身份。';
COMMENT ON COLUMN runtime.run_attempts.lease_until IS '业务时间：以数据库时间判断的执行租约期限。';
COMMENT ON COLUMN runtime.run_attempts.heartbeat_at IS '业务时间：最近一次有效心跳；更新时同时写审计修改字段。';
COMMENT ON COLUMN runtime.run_attempts.workspace_base_revision IS '实际拿到执行权时的工作区起点；适配器校验其归属，不在排队时提前固定。';
COMMENT ON COLUMN runtime.run_attempts.checkpoint_ref IS '本尝试对应的框架检查点/命名空间引用，保留旧 Thread 的审计来源。';
COMMENT ON COLUMN runtime.run_attempts.ended_at IS '业务时间：尝试结束或被接管时间；是否有效以 active_attempt_id/epoch 为准。';
COMMENT ON COLUMN runtime.run_attempts.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.run_attempts.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.tool_executions IS '持久工具操作账本；逻辑调用槽防重复，UNKNOWN 不能盲目重试。';
COMMENT ON COLUMN runtime.tool_executions.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.tool_executions.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.tool_executions.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.tool_executions.logical_call_key IS '绑定 Run 与持久图任务位置的稳定调用槽，不使用每次重推理的新 ID 替代。';
COMMENT ON COLUMN runtime.tool_executions.tool_ref IS '已锁定的工具版本/契约引用。';
COMMENT ON COLUMN runtime.tool_executions.input_ref IS '不可变操作输入存储引用，含 argv/资源/环境引用；不存明文凭据。';
COMMENT ON COLUMN runtime.tool_executions.status IS '当前生命周期状态；通过领域状态机与数据库事务修改。';
COMMENT ON COLUMN runtime.tool_executions.execution_profile_ref IS '实际执行后端配置版本；一期为本机子进程，后续可换沙箱。';
COMMENT ON COLUMN runtime.tool_executions.external_operation_ref IS '外部服务返回的稳定操作句柄，供恢复查询或取消。';
COMMENT ON COLUMN runtime.tool_executions.process_ref IS '本机进程核验元数据，如主机/启动实例/PID/创建时间；单凭 PID 不足以杀进程。';
COMMENT ON COLUMN runtime.tool_executions.result_ref IS '可靠保存的操作结果/日志/文件修订引用。';
COMMENT ON COLUMN runtime.tool_executions.error IS '结构化错误，字段遵循契约；不返回堆栈、令牌或敏感环境。';
COMMENT ON COLUMN runtime.tool_executions.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.tool_executions.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.pending_responses IS '等待澄清或结果核验的请求与幂等回应；approval 仅为后续能力预留。';
COMMENT ON COLUMN runtime.pending_responses.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.pending_responses.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.pending_responses.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.pending_responses.kind IS 'clarification 信息澄清、execution_reconciliation 结果核验、approval 后续审批。';
COMMENT ON COLUMN runtime.pending_responses.payload IS '待回应请求内容，至少包括 prompt；按 kind 处理器校验。';
COMMENT ON COLUMN runtime.pending_responses.response_key IS '上层回应的幂等键；未回应为空，重复相同回应不能再次消费。';
COMMENT ON COLUMN runtime.pending_responses.response_payload IS '已确认的回应内容；新回应写入与状态恢复必须同事务。';
COMMENT ON COLUMN runtime.pending_responses.resolved_at IS '业务时间：回应被确认消费的时间；不等同最后修改时间。';
COMMENT ON COLUMN runtime.pending_responses.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.pending_responses.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.run_events IS '按 Run 序号追加的事件事实；公开发生时间和数据库审计时间分别保留。';
COMMENT ON COLUMN runtime.run_events.event_id IS '事件 UUID 主键；SSE 游标另由 run_id 与 seq 组合。';
COMMENT ON COLUMN runtime.run_events.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.run_events.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.run_events.seq IS 'Run 内正整数事件序号，作为持久回放顺序。';
COMMENT ON COLUMN runtime.run_events.schema_version IS '公开事件结构版本，当前固定为字符串 1。';
COMMENT ON COLUMN runtime.run_events.type IS '公开 EventType 名称，payload 必须匹配对应 OpenAPI schema。';
COMMENT ON COLUMN runtime.run_events.data IS '公开事件 payload；每种 type 的字段由 OpenAPI 封闭定义。';
COMMENT ON COLUMN runtime.run_events.occurred_at IS '业务时间：事件发生时间；与数据库新增/修改审计时间区分。';
COMMENT ON COLUMN runtime.run_events.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.run_events.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.artifacts IS '生成文件/报告等成果引用及来源；专用展示不属于当前主流程。';
COMMENT ON COLUMN runtime.artifacts.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.artifacts.scope_id IS '受信接入层确定的作用域；验证阶段默认为 default，不等于业务用户/角色表。';
COMMENT ON COLUMN runtime.artifacts.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.artifacts.storage_ref IS '存储适配器解析的文件/对象引用；不是任意客户端宿主路径。';
COMMENT ON COLUMN runtime.artifacts.media_type IS '产物 MIME 类型，供后续下载/展示选择处理方式。';
COMMENT ON COLUMN runtime.artifacts.digest IS '成果内容摘要，用于校验和去重关联。';
COMMENT ON COLUMN runtime.artifacts.provenance IS '来源链：Run、数据/脚本/文件版本与生成过程的受限引用。';
COMMENT ON COLUMN runtime.artifacts.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.artifacts.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON INDEX runtime.idx_runs_status_due_created IS '普通索引：按状态、到期时间和接受时间扫描队列。';
COMMENT ON INDEX runtime.idx_runs_session_status_seq IS '普通索引：查询会话状态与任务顺序，不能替代 Session 行锁。';
COMMENT ON INDEX runtime.idx_attempts_ended_lease IS '普通索引：扫描未收尾或过期的执行尝试。';
COMMENT ON INDEX runtime.idx_attempts_run_ended IS '普通索引：按 Run 查执行尝试历史，不能凭此认定执行权。';
COMMENT ON COLUMN runtime.workspaces.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.workspaces.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.workspace_revisions.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.workspace_revisions.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.sessions.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.sessions.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.session_requests.idempotency_key IS '接口幂等键，具体唯一范围由本表唯一约束确定。';
COMMENT ON COLUMN runtime.session_requests.request_fingerprint IS '规范化请求的 SHA-256 摘要；同键不同摘要必须报冲突。';
COMMENT ON COLUMN runtime.session_requests.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.session_requests.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.runs.idempotency_key IS '接口幂等键，具体唯一范围由本表唯一约束确定。';
COMMENT ON COLUMN runtime.runs.request_fingerprint IS '规范化请求的 SHA-256 摘要；同键不同摘要必须报冲突。';
COMMENT ON COLUMN runtime.runs.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.runs.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.run_attempts.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.run_attempts.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.tool_executions.params_digest IS '规范化操作参数 SHA-256 摘要；重复调用槽必须同参数。';
COMMENT ON COLUMN runtime.tool_executions.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.tool_executions.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.pending_responses.response_digest IS '回应内容的 SHA-256 摘要，用于幂等消费校验。';
COMMENT ON COLUMN runtime.pending_responses.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.pending_responses.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.run_events.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.run_events.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMENT ON COLUMN runtime.artifacts.created_by IS '记录创建主体引用；受信 actor 或 system:runtime/<role>，新增后不改写。';
COMMENT ON COLUMN runtime.artifacts.updated_by IS '最后实际修改主体引用；服务动作使用服务身份，幂等空操作不更新。';
COMMIT;
