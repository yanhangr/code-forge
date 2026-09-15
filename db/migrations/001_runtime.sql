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
 scope_id text NOT NULL DEFAULT 'default' CHECK(length(scope_id) <= 4000),
 storage_ref text NOT NULL CHECK(length(storage_ref) <= 4000),
 storage_root text NOT NULL DEFAULT '' CHECK(length(storage_root) <= 4000),
 tenant_ref text NOT NULL CHECK(length(tenant_ref) <= 4000),
 user_ref text NOT NULL CHECK(length(user_ref) <= 4000),
 user_path text NOT NULL CHECK(length(user_path) <= 4000),
 user_rel_path text NOT NULL DEFAULT '' CHECK(length(user_rel_path) <= 4000),
 project_ref text NOT NULL CHECK(length(project_ref) <= 4000),
 project_path text NOT NULL CHECK(length(project_path) <= 4000),
 current_revision uuid,
 active_attempt_id uuid,
 workspace_epoch bigint NOT NULL DEFAULT 0 CHECK(workspace_epoch >= 0),
 lease_until timestamptz,
 heartbeat_at timestamptz,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,id),
 UNIQUE(scope_id,project_ref)
);
CREATE TABLE runtime.workspace_revisions (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL CHECK(length(scope_id) <= 4000),
 workspace_id uuid NOT NULL,
 parent_id uuid,
 manifest_digest text NOT NULL CHECK(length(manifest_digest) <= 4000),
 manifest_ref text NOT NULL CHECK(length(manifest_ref) <= 4000),
 storage_ref text NOT NULL CHECK(length(storage_ref) <= 4000),
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
 scope_id text NOT NULL DEFAULT 'default' CHECK(length(scope_id) <= 4000),
 workspace_id uuid NOT NULL,
 title varchar(200) NOT NULL DEFAULT 'New session',
 external_ref varchar(255),
 thread_id text NOT NULL UNIQUE CHECK(length(thread_id) <= 4000),
 next_run_seq bigint NOT NULL DEFAULT 1 CHECK(next_run_seq > 0),
 next_event_seq bigint NOT NULL DEFAULT 1 CHECK(next_event_seq > 0),
 execution_epoch bigint NOT NULL DEFAULT 0 CHECK(execution_epoch >= 0),
 active_run_id uuid,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,id),
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
 scope_id text NOT NULL DEFAULT 'default' CHECK(length(scope_id) <= 4000),
 session_id uuid NOT NULL,
 workspace_id uuid NOT NULL,
 run_seq bigint NOT NULL CHECK(run_seq > 0),
 idempotency_key varchar(200) NOT NULL CHECK(length(idempotency_key)>0),
 request_fingerprint char(64) NOT NULL,
 input_ref text NOT NULL CHECK(length(input_ref) <= 4000),
 input_digest char(64) NOT NULL,
 input_chars integer NOT NULL CHECK(input_chars > 0),
 agent_ref varchar(200) NOT NULL,
 execution_context jsonb NOT NULL,
 config_snapshot_ref text NOT NULL CHECK(length(config_snapshot_ref) <= 4000),
 config_snapshot_digest char(64) NOT NULL,
 config_snapshot_bytes integer NOT NULL CHECK(config_snapshot_bytes > 0),
 status runtime.run_status NOT NULL DEFAULT 'QUEUED',
 active_attempt_id uuid,
 state_version bigint NOT NULL DEFAULT 0 CHECK(state_version >= 0),
 task_outcome runtime.task_outcome,
 wait_reason varchar(200),
 output_ref text CHECK(output_ref IS NULL OR length(output_ref) <= 4000),
 output_digest char(64),
 output_chars integer CHECK(output_chars IS NULL OR output_chars > 0),
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
 FOREIGN KEY(scope_id,workspace_id) REFERENCES runtime.workspaces(scope_id,id),
 CHECK((status='SUCCEEDED') = (task_outcome IS NOT NULL)),
 CHECK((status IN ('WAITING_USER','WAITING_EXTERNAL')) = (wait_reason IS NOT NULL)),
 CHECK((output_ref IS NULL) = (output_digest IS NULL)),
 CHECK((output_ref IS NULL) = (output_chars IS NULL)),
 CHECK(octet_length(execution_context::text) <= 4000),
 CHECK(error IS NULL OR octet_length(error::text) <= 4000)
);
CREATE INDEX idx_runs_status_due_created ON runtime.runs(status,due_at,date_created);
CREATE INDEX idx_runs_scope_status_due_created ON runtime.runs(scope_id,status,due_at,date_created);
CREATE INDEX idx_runs_session_status_seq ON runtime.runs(scope_id,session_id,status,run_seq);
ALTER TABLE runtime.sessions ADD CONSTRAINT session_active_run_fk
 FOREIGN KEY(scope_id,id,active_run_id) REFERENCES runtime.runs(scope_id,session_id,id);

CREATE TABLE runtime.run_attempts (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL CHECK(length(scope_id) <= 4000),
 session_id uuid NOT NULL,
 run_id uuid NOT NULL,
 workspace_id uuid NOT NULL,
 epoch bigint NOT NULL CHECK(epoch>0),
 workspace_epoch bigint NOT NULL CHECK(workspace_epoch>0),
 worker_id text NOT NULL CHECK(length(worker_id) <= 4000),
 lease_until timestamptz NOT NULL,
 heartbeat_at timestamptz NOT NULL,
 workspace_base_revision uuid,
 mount_spec_ref text NOT NULL CHECK(length(mount_spec_ref) <= 4000),
 working_directory_ref text NOT NULL CHECK(length(working_directory_ref) <= 4000),
 checkpoint_ref text CHECK(checkpoint_ref IS NULL OR length(checkpoint_ref) <= 4000),
 ended_at timestamptz,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,session_id,epoch),
 UNIQUE(scope_id,run_id,id),
 UNIQUE(scope_id,workspace_id,workspace_epoch),
 UNIQUE(scope_id,workspace_id,id),
 FOREIGN KEY(scope_id,session_id,run_id) REFERENCES runtime.runs(scope_id,session_id,id),
 FOREIGN KEY(scope_id,workspace_id) REFERENCES runtime.workspaces(scope_id,id)
 -- Adapter validates workspace_base_revision belongs to Session workspace.
);
CREATE INDEX idx_attempts_ended_lease ON runtime.run_attempts(ended_at,lease_until);
CREATE INDEX idx_attempts_run_ended ON runtime.run_attempts(scope_id,run_id,ended_at);
ALTER TABLE runtime.runs ADD CONSTRAINT run_active_attempt_fk
 FOREIGN KEY(scope_id,id,active_attempt_id) REFERENCES runtime.run_attempts(scope_id,run_id,id);
ALTER TABLE runtime.workspaces ADD CONSTRAINT workspace_active_attempt_fk
 FOREIGN KEY(scope_id,id,active_attempt_id) REFERENCES runtime.run_attempts(scope_id,workspace_id,id);

CREATE TABLE runtime.tool_executions (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL CHECK(length(scope_id) <= 4000),
 run_id uuid NOT NULL,
 workspace_id uuid NOT NULL,
 logical_call_key text NOT NULL CHECK(length(logical_call_key) <= 4000),
 tool_ref text NOT NULL CHECK(length(tool_ref) <= 4000),
 params_digest char(64) NOT NULL,
 input_ref text NOT NULL CHECK(length(input_ref) <= 4000),
 input_digest char(64),
 input_bytes integer CHECK(input_bytes IS NULL OR input_bytes > 0),
 status runtime.tool_status NOT NULL DEFAULT 'PREPARED',
 execution_profile_ref text NOT NULL CHECK(length(execution_profile_ref) <= 4000),
 external_operation_ref text CHECK(external_operation_ref IS NULL OR length(external_operation_ref) <= 4000),
 process_ref jsonb,
 result_ref text CHECK(result_ref IS NULL OR length(result_ref) <= 4000),
 input_revision_id uuid,
 result_revision_id uuid,
 error jsonb,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,run_id,id),
 UNIQUE(scope_id,run_id,logical_call_key),
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id),
 FOREIGN KEY(scope_id,workspace_id) REFERENCES runtime.workspaces(scope_id,id),
 FOREIGN KEY(scope_id,workspace_id,input_revision_id)
  REFERENCES runtime.workspace_revisions(scope_id,workspace_id,id),
 FOREIGN KEY(scope_id,workspace_id,result_revision_id)
  REFERENCES runtime.workspace_revisions(scope_id,workspace_id,id),
 CHECK(process_ref IS NULL OR octet_length(process_ref::text) <= 4000),
 CHECK(error IS NULL OR octet_length(error::text) <= 4000)
);
CREATE TABLE runtime.pending_responses (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL CHECK(length(scope_id) <= 4000),
 run_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('clarification','execution_reconciliation','approval')),
 payload_ref text NOT NULL CHECK(length(payload_ref) <= 4000),
 payload_digest char(64) NOT NULL,
 payload_bytes integer NOT NULL CHECK(payload_bytes > 0),
 response_key text CHECK(response_key IS NULL OR length(response_key) <= 4000),
 response_digest char(64),
 response_payload_ref text CHECK(response_payload_ref IS NULL OR length(response_payload_ref) <= 4000),
 response_payload_digest char(64),
 response_payload_bytes integer CHECK(response_payload_bytes IS NULL OR response_payload_bytes > 0),
 resolved_at timestamptz,
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,run_id,id),
 UNIQUE(scope_id,run_id,response_key),
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id),
 CHECK((response_payload_ref IS NULL) = (response_payload_digest IS NULL)),
 CHECK((response_payload_ref IS NULL) = (response_payload_bytes IS NULL))
);
-- Approval is a reserved protocol kind, not a requirement to build an approval product.
CREATE TABLE runtime.run_events (
 event_id uuid PRIMARY KEY,
 scope_id text NOT NULL CHECK(length(scope_id) <= 4000),
 run_id uuid NOT NULL,
 session_id uuid NOT NULL,
 seq bigint NOT NULL CHECK(seq>0),
 session_seq bigint NOT NULL CHECK(session_seq>0),
 schema_version text NOT NULL DEFAULT '1' CHECK(schema_version='1'),
 type text NOT NULL CHECK(length(type) <= 4000),
 data_ref text NOT NULL CHECK(length(data_ref) <= 4000),
 data_offset bigint NOT NULL CHECK(data_offset >= 0),
 data_bytes integer NOT NULL CHECK(data_bytes > 0),
 data_digest char(64) NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT now(),
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 UNIQUE(scope_id,run_id,seq),
 FOREIGN KEY(scope_id,session_id,run_id) REFERENCES runtime.runs(scope_id,session_id,id)
);
CREATE UNIQUE INDEX idx_events_scope_session_session_seq ON runtime.run_events(scope_id,session_id,session_seq);
CREATE TABLE runtime.artifacts (
 id uuid PRIMARY KEY,
 scope_id text NOT NULL CHECK(length(scope_id) <= 4000),
 run_id uuid NOT NULL,
 workspace_id uuid NOT NULL,
 storage_ref text NOT NULL CHECK(length(storage_ref) <= 4000),
 media_type text NOT NULL CHECK(length(media_type) <= 4000),
 digest text NOT NULL CHECK(length(digest) <= 4000),
 provenance jsonb NOT NULL DEFAULT '{}',
 date_created timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_by varchar(200) NOT NULL,
 date_updated timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_by varchar(200) NOT NULL,
 FOREIGN KEY(scope_id,run_id) REFERENCES runtime.runs(scope_id,id),
 FOREIGN KEY(scope_id,workspace_id) REFERENCES runtime.workspaces(scope_id,id),
 CHECK(octet_length(provenance::text) <= 4000)
);
-- LangGraph checkpoint tables are managed by its pinned saver migration.
-- Require a scoped/fenced adapter; do not serialize custom graph state into runs.output.

-- 数据字典：所有自管表及字段必须有 COMMENT。
COMMENT ON TABLE runtime.workspaces IS '持久工作区；current_revision 指向已可靠提交的文件修订。';
COMMENT ON COLUMN runtime.workspaces.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.workspaces.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref，验证阶段默认为 default。';
COMMENT ON COLUMN runtime.workspaces.storage_ref IS '存储适配器解析的文件/对象引用；不是任意客户端宿主路径。';
COMMENT ON COLUMN runtime.workspaces.storage_root IS '稳定 NAS 挂载根；迁移时优先保持逻辑根不变。';
COMMENT ON COLUMN runtime.workspaces.tenant_ref IS 'Platform 管理的租户稳定引用，不保存租户业务表和角色。';
COMMENT ON COLUMN runtime.workspaces.user_ref IS 'Platform 管理的用户稳定引用，也是本工作区的 scope_id。';
COMMENT ON COLUMN runtime.workspaces.user_path IS 'Platform 下发的用户根路径；用户隔离和默认 Skill 路径的基准。';
COMMENT ON COLUMN runtime.workspaces.user_rel_path IS '相对 storage_root 的用户逻辑路径，是稳定用户身份的一部分。';
COMMENT ON COLUMN runtime.workspaces.project_ref IS 'Platform 管理的项目稳定引用；同一用户可绑定多个项目工作区。';
COMMENT ON COLUMN runtime.workspaces.project_path IS '项目执行根路径，必须位于 user_path 下；Agent 默认 cwd 和可写根。';
COMMENT ON COLUMN runtime.workspaces.current_revision IS '已提交修订指针；按预期父修订 CAS 更新。';
COMMENT ON COLUMN runtime.workspaces.active_attempt_id IS '当前唯一有效执行尝试；Workspace 级 writer lease 的持有指针。';
COMMENT ON COLUMN runtime.workspaces.workspace_epoch IS 'Workspace 单调递增执行代号；旧 Attempt 写入必须被拒绝。';
COMMENT ON COLUMN runtime.workspaces.lease_until IS '业务时间：当前 Workspace writer lease 到期时间。';
COMMENT ON COLUMN runtime.workspaces.heartbeat_at IS '业务时间：当前 Workspace writer 最近心跳时间。';
COMMENT ON COLUMN runtime.workspaces.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.workspaces.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.workspace_revisions IS '不可变文件修订链；内容先可靠保存，再提交 manifest 引用。';
COMMENT ON COLUMN runtime.workspace_revisions.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.workspace_revisions.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.workspace_revisions.workspace_id IS '所属工作区 UUID；必须与 scope_id 匹配。';
COMMENT ON COLUMN runtime.workspace_revisions.parent_id IS '父修订 UUID；首修订为空，不允许关联其他工作区。';
COMMENT ON COLUMN runtime.workspace_revisions.manifest_digest IS '文件清单的内容摘要，用于完整性校验。';
COMMENT ON COLUMN runtime.workspace_revisions.manifest_ref IS '不可变文件清单的存储引用，提交前确保其内容可读取。';
COMMENT ON COLUMN runtime.workspace_revisions.storage_ref IS '该修订不可变文件副本的存储引用。';
COMMENT ON COLUMN runtime.workspace_revisions.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.workspace_revisions.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.sessions IS '连续会话与调度门；锁本行管理主 Run 和 Session 单调执行代号。';
COMMENT ON COLUMN runtime.sessions.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.sessions.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.sessions.workspace_id IS '所属工作区 UUID；必须与 scope_id 匹配。';
COMMENT ON COLUMN runtime.sessions.title IS '会话展示标题，不参与推理或授权决策。';
COMMENT ON COLUMN runtime.sessions.external_ref IS '上层业务的可选关联标识，不作为授权凭据。';
COMMENT ON COLUMN runtime.sessions.thread_id IS '当前 LangGraph Thread 标识；产品 Session ID 不随恢复换代改变。';
COMMENT ON COLUMN runtime.sessions.next_run_seq IS '下次接受 Run 使用的正整数序号；在 Session 行锁事务中分配。';
COMMENT ON COLUMN runtime.sessions.next_event_seq IS '下一个 Session 级公开事件序号；用于 SSE 断线恢复。';
COMMENT ON COLUMN runtime.sessions.execution_epoch IS 'Session 单调递增执行代号；领取/接管时增加，旧写入必须拒绝。';
COMMENT ON COLUMN runtime.sessions.active_run_id IS 'Session 当前主任务指针，等待时保留；终态收尾时清空。';
COMMENT ON COLUMN runtime.sessions.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.sessions.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.session_requests IS '创建会话的幂等请求；同作用域同键只能关联一个确定请求和会话。';
COMMENT ON COLUMN runtime.session_requests.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.session_requests.session_id IS '所属 Session UUID，必须属于相同 scope。';
COMMENT ON COLUMN runtime.session_requests.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.session_requests.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.runs IS '一次被接受的 Agent 任务；配置快照不可变，状态和事件在同一事务提交。';
COMMENT ON COLUMN runtime.runs.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.runs.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.runs.session_id IS '所属 Session UUID，必须属于相同 scope。';
COMMENT ON COLUMN runtime.runs.workspace_id IS 'Run 绑定的唯一用户项目 Workspace，必须属于相同 user scope。';
COMMENT ON COLUMN runtime.runs.run_seq IS 'Session 内 Run 接受顺序；网络重试不重复分配。';
COMMENT ON COLUMN runtime.runs.input_ref IS '用户输入正文的不可变内容引用；正文在文件/JSONL，PG 不存大文本。';
COMMENT ON COLUMN runtime.runs.input_digest IS '用户输入正文的 SHA-256 摘要，读取时校验完整性。';
COMMENT ON COLUMN runtime.runs.input_chars IS '用户输入字符数，用于展示与校验，不代替正文。';
COMMENT ON COLUMN runtime.runs.agent_ref IS '提交时选择的 Agent 配置引用，例如 general@1。';
COMMENT ON COLUMN runtime.runs.execution_context IS '作用域及调用主体引用；不得保存真实密钥，结构按公开契约校验。';
COMMENT ON COLUMN runtime.runs.config_snapshot_ref IS '不可变 RunSnapshot 的存储引用；正文在文件，PG 只存引用/摘要/长度。';
COMMENT ON COLUMN runtime.runs.config_snapshot_digest IS 'RunSnapshot 文件的 SHA-256 摘要，读取时校验。';
COMMENT ON COLUMN runtime.runs.config_snapshot_bytes IS 'RunSnapshot 文件字节数，用于完整性校验。';
COMMENT ON COLUMN runtime.runs.status IS '当前生命周期状态；通过领域状态机与数据库事务修改。';
COMMENT ON COLUMN runtime.runs.active_attempt_id IS 'Run 唯一有效执行尝试指针；锁 Session/Run 并验证 epoch 后更新，不能仅按 ended_at 查询执行权。';
COMMENT ON COLUMN runtime.runs.state_version IS '状态乐观并发版本；每次合法状态更新递增，0 行 CAS 表示冲突。';
COMMENT ON COLUMN runtime.runs.task_outcome IS '正常协议结束时的 completed/partial/blocked；不等同测试或业务验收通过。';
COMMENT ON COLUMN runtime.runs.wait_reason IS 'WAITING_USER/WAITING_EXTERNAL 的等待原因，其他状态必须为空。';
COMMENT ON COLUMN runtime.runs.output_ref IS '最终回复正文的内容引用；无回复时为空，正文在文件。';
COMMENT ON COLUMN runtime.runs.output_digest IS '最终回复正文的 SHA-256 摘要，与 output_ref 同时为空或非空。';
COMMENT ON COLUMN runtime.runs.output_chars IS '最终回复字符数；不代替正文，也不代替工具执行事实。';
COMMENT ON COLUMN runtime.runs.error IS '结构化错误，字段遵循契约；不返回堆栈、令牌或敏感环境。';
COMMENT ON COLUMN runtime.runs.cancel_requested_at IS '业务时间：第一次收到有效取消请求的时间，幂等重试不覆盖。';
COMMENT ON COLUMN runtime.runs.deadline_at IS '业务时间：Run 总期限，可空；超时清理和未知操作核验按流程执行。';
COMMENT ON COLUMN runtime.runs.due_at IS '业务时间：下一次可调度时间，用于排队或退避唤醒。';
COMMENT ON COLUMN runtime.runs.next_event_seq IS '下一事件序号；在锁定 Run 的事务中分配，已提交序号不能复用。';
COMMENT ON COLUMN runtime.runs.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.runs.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.run_attempts IS 'Run 的执行尝试/接管记录；唯一有效尝试由 runs.active_attempt_id 与 epoch 校验确定。';
COMMENT ON COLUMN runtime.run_attempts.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.run_attempts.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.run_attempts.session_id IS '所属 Session UUID，必须属于相同 scope。';
COMMENT ON COLUMN runtime.run_attempts.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.run_attempts.workspace_id IS 'Attempt 绑定的 Workspace UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.run_attempts.epoch IS '该尝试领取时的 Session execution_epoch，失效后不得提交状态/检查点。';
COMMENT ON COLUMN runtime.run_attempts.workspace_epoch IS '该尝试领取时的 Workspace writer epoch，旧值不得发布修订。';
COMMENT ON COLUMN runtime.run_attempts.worker_id IS '具体应用实例/执行者标识；不同进程不能共用同一身份。';
COMMENT ON COLUMN runtime.run_attempts.lease_until IS '业务时间：以数据库时间判断的执行租约期限。';
COMMENT ON COLUMN runtime.run_attempts.heartbeat_at IS '业务时间：最近一次有效心跳；更新时同时写审计修改字段。';
COMMENT ON COLUMN runtime.run_attempts.workspace_base_revision IS '实际拿到执行权时的工作区起点；适配器校验其归属，不在排队时提前固定。';
COMMENT ON COLUMN runtime.run_attempts.mount_spec_ref IS '本次执行的 MountSpec 版本化引用；V1 保留 Project/SPACE/REVISION 粒度。';
COMMENT ON COLUMN runtime.run_attempts.working_directory_ref IS '本次执行固定在项目路径下的工作目录引用。';
COMMENT ON COLUMN runtime.run_attempts.checkpoint_ref IS '本尝试对应的框架检查点/命名空间引用，保留旧 Thread 的审计来源。';
COMMENT ON COLUMN runtime.run_attempts.ended_at IS '业务时间：尝试结束或被接管时间；是否有效以 active_attempt_id/epoch 为准。';
COMMENT ON COLUMN runtime.run_attempts.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.run_attempts.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.tool_executions IS '持久工具操作账本；逻辑调用槽防重复，UNKNOWN 不能盲目重试。';
COMMENT ON COLUMN runtime.tool_executions.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.tool_executions.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.tool_executions.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.tool_executions.workspace_id IS '工具操作绑定的 Workspace UUID；与 Run 和 scope 一致。';
COMMENT ON COLUMN runtime.tool_executions.logical_call_key IS '绑定 Run 与持久图任务位置的稳定调用槽，不使用每次重推理的新 ID 替代。';
COMMENT ON COLUMN runtime.tool_executions.tool_ref IS '已锁定的工具版本/契约引用。';
COMMENT ON COLUMN runtime.tool_executions.input_ref IS '不可变操作输入存储引用，含 argv/资源/环境引用；不存明文凭据。';
COMMENT ON COLUMN runtime.tool_executions.input_digest IS '操作输入对象的 SHA-256 摘要，用于幂等与核验。';
COMMENT ON COLUMN runtime.tool_executions.input_bytes IS '操作输入对象字节数，用于完整性校验。';
COMMENT ON COLUMN runtime.tool_executions.status IS '当前生命周期状态；通过领域状态机与数据库事务修改。';
COMMENT ON COLUMN runtime.tool_executions.execution_profile_ref IS '实际执行后端配置版本；一期为本机子进程，后续可换沙箱。';
COMMENT ON COLUMN runtime.tool_executions.external_operation_ref IS '外部服务返回的稳定操作句柄，供恢复查询或取消。';
COMMENT ON COLUMN runtime.tool_executions.process_ref IS '本机进程核验元数据，如主机/启动实例/PID/创建时间；单凭 PID 不足以杀进程。';
COMMENT ON COLUMN runtime.tool_executions.result_ref IS '可靠保存的操作结果/日志/文件修订引用。';
COMMENT ON COLUMN runtime.tool_executions.input_revision_id IS '工具开始前的工作区修订；无修订时为空。';
COMMENT ON COLUMN runtime.tool_executions.result_revision_id IS '工具成功提交后的工作区修订；失败或未提交时为空。';
COMMENT ON COLUMN runtime.tool_executions.error IS '结构化错误，字段遵循契约；不返回堆栈、令牌或敏感环境。';
COMMENT ON COLUMN runtime.tool_executions.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.tool_executions.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.pending_responses IS '等待澄清或结果核验的请求与幂等回应；approval 仅为后续能力预留。';
COMMENT ON COLUMN runtime.pending_responses.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.pending_responses.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.pending_responses.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.pending_responses.kind IS 'clarification 信息澄清、execution_reconciliation 结果核验、approval 后续审批。';
COMMENT ON COLUMN runtime.pending_responses.payload_ref IS '待回应请求正文的内容引用；正文在文件，至少包括 prompt。';
COMMENT ON COLUMN runtime.pending_responses.payload_digest IS '待回应请求正文的 SHA-256 摘要，回应写入前校验。';
COMMENT ON COLUMN runtime.pending_responses.payload_bytes IS '待回应请求正文字节数。';
COMMENT ON COLUMN runtime.pending_responses.response_key IS '上层回应的幂等键；未回应为空，重复相同回应不能再次消费。';
COMMENT ON COLUMN runtime.pending_responses.response_payload_ref IS '已确认回应正文的内容引用；正文在文件，与状态恢复同事务。';
COMMENT ON COLUMN runtime.pending_responses.response_payload_digest IS '已确认回应正文的 SHA-256 摘要，用于幂等消费校验。';
COMMENT ON COLUMN runtime.pending_responses.response_payload_bytes IS '已确认回应正文字节数。';
COMMENT ON COLUMN runtime.pending_responses.resolved_at IS '业务时间：回应被确认消费的时间；不等同最后修改时间。';
COMMENT ON COLUMN runtime.pending_responses.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.pending_responses.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.run_events IS '按 Run 序号追加的事件事实；公开发生时间和数据库审计时间分别保留。';
COMMENT ON COLUMN runtime.run_events.event_id IS '事件 UUID 主键；SSE 游标另由 run_id 与 seq 组合。';
COMMENT ON COLUMN runtime.run_events.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.run_events.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.run_events.session_id IS '所属 Session UUID，用于 Session 级事件投影。';
COMMENT ON COLUMN runtime.run_events.seq IS 'Run 内正整数事件序号，作为持久回放顺序。';
COMMENT ON COLUMN runtime.run_events.session_seq IS 'Session 内正整数公开事件序号，用于 Platform SSE 游标。';
COMMENT ON COLUMN runtime.run_events.schema_version IS '公开事件结构版本，当前固定为字符串 1。';
COMMENT ON COLUMN runtime.run_events.type IS '公开 EventType 名称，payload 必须匹配对应 OpenAPI schema。';
COMMENT ON COLUMN runtime.run_events.data_ref IS '事件正文所在的 Session JSONL 记录引用；PG 不存大文本 payload。';
COMMENT ON COLUMN runtime.run_events.data_offset IS '事件正文记录在 JSONL 文件中的起始字节偏移。';
COMMENT ON COLUMN runtime.run_events.data_bytes IS '事件正文记录的字节数，用于按偏移精确读取。';
COMMENT ON COLUMN runtime.run_events.data_digest IS '事件正文的 SHA-256 摘要，回放读取时校验完整性。';
COMMENT ON COLUMN runtime.run_events.occurred_at IS '业务时间：事件发生时间；与数据库新增/修改审计时间区分。';
COMMENT ON COLUMN runtime.run_events.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.run_events.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON TABLE runtime.artifacts IS '生成文件/报告等成果引用及来源；专用展示不属于当前主流程。';
COMMENT ON COLUMN runtime.artifacts.id IS '应用生成的 UUID 主键。';
COMMENT ON COLUMN runtime.artifacts.scope_id IS '受信接入层确定的用户执行作用域；等于 user_ref。';
COMMENT ON COLUMN runtime.artifacts.run_id IS '所属 Run UUID，必须匹配 scope。';
COMMENT ON COLUMN runtime.artifacts.workspace_id IS '产物绑定的 Workspace UUID；用于用户项目访问和修订追踪。';
COMMENT ON COLUMN runtime.artifacts.storage_ref IS '存储适配器解析的文件/对象引用；不是任意客户端宿主路径。';
COMMENT ON COLUMN runtime.artifacts.media_type IS '产物 MIME 类型，供后续下载/展示选择处理方式。';
COMMENT ON COLUMN runtime.artifacts.digest IS '成果内容摘要，用于校验和去重关联。';
COMMENT ON COLUMN runtime.artifacts.provenance IS '来源链：Run、数据/脚本/文件版本与生成过程的受限引用。';
COMMENT ON COLUMN runtime.artifacts.date_created IS '记录新增时间，timestamptz；使用数据库时间，新增后不改写。';
COMMENT ON COLUMN runtime.artifacts.date_updated IS '记录最后实际修改时间，timestamptz；每次业务写入同步更新，幂等空操作不更新。';
COMMENT ON INDEX runtime.idx_runs_status_due_created IS '普通索引：按状态、到期时间和接受时间扫描队列。';
COMMENT ON INDEX runtime.idx_runs_scope_status_due_created IS '普通索引：按用户 scope 扫描到期队列。';
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
