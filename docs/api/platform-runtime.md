# Platform ↔ Agent Runtime 接入契约 v1（文档修订0.2）

日期：2026-09-10。状态：设计契约，服务端与页面尚待实现。字段/类型以 [OpenAPI](openapi.json) 为准；行为以本文为准。示例 ID 为 UUID，密钥不属于公开请求或事件字段。

Session/Run 资源读接口统一包含只读审计字段 date_created、created_by、date_updated、updated_by，时间为带时区的 ISO 8601。客户端不得提交它们；业务事件 occurred_at 保持原语义。当前没有已部署客户端，本次直接修订契约；未来不兼容变化需要版本迁移。

## 1. 当前边界与默认值

Runtime 基址示例为 http://127.0.0.1:8000，最小 Platform 是另一个应用。当前为可信内部默认授权验证，不要求登录/角色/审批。默认 scope_id=default，X-Forge-Scope 默认为 default；它不是认证凭据，后续受信接入层需要验证其来源。

所有 v1 资源按 X-Forge-Scope 解析。POST 中 context.scope_id 未给出时由该请求头填充；两者均明确给出但不同返回 INVALID_REQUEST。actor_ref/external_ref 可用于业务关联，不能替代未来授权。GET/取消等没有 Context 请求体时仍用该作用域头。

Platform 不允许通过 Runtime 响应推测生产权限已经就绪。验证服务默认只绑定本机或受控内部网络，跨站访问控制由实现阶段落实；不得为了页面联调无约束公开可执行命令的入口。

## 2. 最小交互流程

1. GET /health：显示 Runtime 可达、模型是否已配置、执行后端和默认授权模式。模型未配置不能伪装成真实回答。
2. GET /v1/skills：列出手工 Skill 的摘要与当前版本；页面只选择，不编辑发布。
3. POST /v1/sessions：创建会话。Idempotency-Key 用于网络重试，响应 201 为 Session。
4. POST /v1/sessions/{id}/runs：input 必填，agent_ref 默认 general@1；skills 空数组表示没有 Skill 候选。返回 202 AcceptedRun，不等待模型完成。
5. GET /v1/runs/{id}/events：按 SSE 显示消息/工具进展，run.finished 后读取最终状态。
6. 用户追问创建新 Run；若当前等待澄清，提交 /responses 恢复原 Run。
7. 页面刷新先读 /snapshot 和当前会话 Run 列表，再按事件游标续订。

代码/分析是否成功由真实工具输出与 task_outcome 判断；HTTP 202 只是接受，Run SUCCEEDED 只是正常完成输出协议，不能直接渲染为“测试通过”。

## 3. 提交与幂等

创建 Run 示例：

```http
POST /v1/sessions/11111111-1111-4111-8111-111111111111/runs
Content-Type: application/json
Idempotency-Key: browser-message-0001
X-Forge-Scope: default
```

```json
{
  "input": "使用 Python 计算 1 到 100 的平方和，保存结果并告诉我文件名",
  "agent_ref": "general@1",
  "skills": [{"name": "python-analysis", "version": "1"}],
  "context": {"scope_id": "default", "actor_ref": null, "external_ref": "ui-message-0001"}
}
```

```json
{
  "id": "22222222-2222-4222-8222-222222222222",
  "session_id": "11111111-1111-4111-8111-111111111111",
  "status": "QUEUED",
  "state_version": 0,
  "reused": false
}
```

Run 幂等范围为 `(scope_id, session_id, Idempotency-Key)`。正文经过传输层缺省值补齐后，用核心 request_fingerprint 计算；保留 input 空白和 Skill 顺序。同键同摘要复用原 Run，即使 Skill 源文件已修改，也不重新解析；不同摘要返回 409 IDEMPOTENCY_CONFLICT。复用时 status/state_version 可以已前进，202 不意味着它仍在排队。

Session 创建幂等范围为 `(scope_id, Idempotency-Key)`，由 session_requests 记录规范化请求摘要。不同业务操作不共享请求键命名空间。用户明确提交新任务生成新键；超时重试原 HTTP 请求复用原键。

## 4. 事件与断线恢复

每个事件信封含 event_id、run_id、seq、schema_version、type、occurred_at、data。事件名与各自 data 的封闭 schema 均在 OpenAPI 中，不由前端自行猜测。SSE 的 id 为 `run_id:seq`，event 为 type，data 为整个事件 JSON。

```text
id: 22222222-2222-4222-8222-222222222222:4
event: tool.output
data: {"event_id":"33333333-3333-4333-8333-333333333333","run_id":"22222222-2222-4222-8222-222222222222","seq":4,"schema_version":"1","type":"tool.output","occurred_at":"2026-09-10T00:00:00Z","data":{"operation_id":"44444444-4444-4444-8444-444444444444","stream":"stdout","text":"338350\n","truncated":false}}

```

- Run 内 seq 严格递增，时间戳不是顺序依据。客户端按 run_id+seq 去重，允许重连时收到重复事件。
- 首次从 0 开始；恢复使用 Last-Event-ID 或 after_seq，二者同时出现采用同 Run 的较大值。不同 Run 的游标返回 INVALID_EVENT_CURSOR。
- /snapshot 返回同一数据库一致性快照中的 Run、待回应项和 event_cursor。前端先应用快照，再订阅大于该游标的事件；不能先读消息再独立取水位而漏中间变化。
- 先补发历史，再继续监听；建立通知与补发之间要再次查库水位，避免订阅竞态。数据库事件可轮询，Redis 非当前必需。
- message.delta 是增量；message.completed 携带该 message_id 的完整文本，替换临时拼接内容。不能把完整文本再次追加一遍。
- tool.output 是标准输出/错误，不是模型回复，也不是隐藏推理。只展示有界输出；truncated=true 时不能声称日志完整。
- run.finished 为终态事件，前端关闭 EventSource，避免终态后浏览器自动重连死循环。断线本身不取消 Run。
- 当前验证阶段事件随 Run 保留，不裁剪单个 Run 中段历史。未来引入保留窗口时必须增加明确的快照恢复语义，不能静默漏事件。
- SSE 前校验错误用 JSON 错误返回；流开启后执行错误以 run.finished/error 或工具事件表达，不能在 SSE 中间塞普通 HTTP 错误页。

分页列表采用 items/next_cursor，cursor 不透明，按创建时间与 ID 稳定排序；Run 列表按 run_seq 正序。event-history 按 seq 正序，返回 items/next_after_seq，下一页传 after_seq；不要把事件分页字段混用为普通 cursor。

## 5. 取消、等待与重新执行

POST /cancel 幂等，不要求额外请求键。已终态返回原 Run；执行中进入 CANCELLING，停止新工具并清理/核验已有进程。仅收到取消请求不意味着 Python 已退出。最终 CANCELLED 也不撤销已产生的文件或远端副作用，结果未知必须保留证据。

信息澄清通过 run.waiting 的 pending_response 展示；响应带 pending_id、response_key、expected_state_version 和 text。先验证待决项属于该 Run，再查 response_key 的既有消费记录：同摘要重复回应返回当前结果，不因原 expected_state_version 已过期报错。新回应才检查待决项仍可回应与状态版本，并事务性消费。不同摘要或新回应的错误状态返回冲突。

execution_reconciliation 用于进程结果未知。用户文本不能被直接当作“重跑授权”；Runtime 的核验处理器先确认原操作和当前文件状态，再给出受控恢复/结束动作。首个版本可以要求用户结束原 Run 并新建任务，必须显式说明，不能后台盲重放。

approval kind 仅预留；当前默认授权不产生普通工具审批。实现者不能因此要求开发审批中心后主流程才能运行。

## 6. 文件、错误与扩展

文件接口仅供实现验证，最小页面不要求文件管理或报告预览。path 是 Workspace 相对路径，拒绝绝对路径、目录穿越和符号链接逃逸；文本读取有大小上限，二进制返回可识别错误或后续产物下载接口，不能以错误解码内容冒充文本。

统一错误字段：error.code/message/retryable/request_id。request_id 是诊断关联，不是幂等键。不得返回堆栈、模型密钥或进程环境。

| HTTP | 典型错误码 | Platform 行为 |
| --- | --- | --- |
| 400 / 422 | INVALID_REQUEST、INVALID_EVENT_CURSOR、SKILL_INCOMPATIBLE | 提示修正输入，不自动无限重试 |
| 403 | CAPABILITY_DENIED | 展示拒绝原因，当前默认授权阶段仍保留契约 |
| 404 | SESSION_NOT_FOUND、RUN_NOT_FOUND、SKILL_NOT_FOUND | 刷新资源选择，不读取其他作用域 |
| 409 | IDEMPOTENCY_CONFLICT、STATE_CONFLICT | 保留用户输入，重新读取状态，不能自动换键重提写任务 |
| 429 | RESOURCE_LIMIT | 有界退避/排队提示 |
| 503 | MODEL_NOT_CONFIGURED、DEPENDENCY_UNAVAILABLE | 明确当前无法执行，不能用假模型代替真实成功 |

EXECUTION_FAILED/EXECUTION_UNKNOWN 主要在工具/Run 结果内出现；HTTP 任务查询仍可正常返回该失败事实。增加错误码或事件必须同步核心代码、生成器和文档，再由实现方使用。
