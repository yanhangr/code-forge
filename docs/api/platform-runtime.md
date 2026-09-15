# Platform ↔ Agent Runtime 接口规范 v1

日期：2026-09-14。状态：唯一接口规范。

本文是真实 Platform 服务端开发者的接口入口。Runtime 内部可以使用 Run、Attempt 等实现
对象，但 Platform 对外只看到 Session 和 Message。

## 1. 最短调用顺序

```text
1. POST /v1/create-session
   -> 取得 session_id

2. POST /v1/send-message
   建立 SSE，首个事件 message.accepted 返回 message_id
   后续在同一连接接收执行事件，直到 message.finished

3. 如果当前 Message 需要用户回应
   POST /v1/reply
   继续通过同一 SSE 接收恢复后的事件

4. 如果需要终止当前 Message
   POST /v1/cancel-message
   等待正在使用的 SSE 或重连流收到 message.finished

5. 页面刷新或网络断开
   GET /v1/get-session-state
   -> 取得 event_cursor
   GET /v1/stream-session-events
   -> 从 event_cursor 之后恢复订阅

6. 查询历史
   GET /v1/get-message
   GET /v1/list-session-messages
```

接口优先级：

| 级别 | 接口 | 说明 |
| --- | --- | --- |
| 核心写 | `create-session`、`update-session`、`send-message`、`cancel-message`、`reply` | 主流程；发送和回复直接返回 SSE，终止返回当前 Message 状态。 |
| 核心读 | `get-session-state`、`get-message` | 状态查询。 |
| 断线恢复 | `stream-session-events` | 只用于页面刷新或网络重连。 |
| 历史恢复 | `get-session`、`list-sessions`、`list-session-messages`、`list-session-events` | 刷新、翻页和补偿。 |
| 诊断 | `health`、`list-session-files`、`read-session-file` | 不作为主链路依赖。 |

## 2. 统一响应格式

所有普通 HTTP 接口统一返回：

```json
{
  "code": "0000",
  "message": "success",
  "data": {}
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `code` | string | 业务结果码。成功固定为 `0000`；失败使用下表的错误码。 |
| `message` | string | 面向调用方的简短说明。成功固定为 `success`。 |
| `data` | object | 接口业务数据；没有数据时返回空对象 `{}`。 |

错误示例：

```json
{
  "code": "1001",
  "message": "storage_root must be an absolute path",
  "data": {}
}
```

业务码：

| code | HTTP | 含义 |
| --- | --- | --- |
| `0000` | 200/201/202 | 成功。 |
| `1001` | 400 | 请求字段或格式不合法。 |
| `1002` | 404 | Session 不存在。 |
| `1003` | 404 | Message 不存在。 |
| `1004` | 409 | 状态冲突。 |
| `1005` | 409 | Session 已有 Message 正在执行，Platform 应自行排队。 |
| `2001` | 422 | Skill 路径或 Skill 包不合法。 |
| `3001` | 200/503 | 执行确定失败。 |
| `3002` | 200/503 | 外部效果未知，需要核验。 |
| `4001` | 503 | 模型不可用。 |
| `5001` | 503 | 依赖不可用。 |
| `9001` | 500 | Runtime 内部错误。 |

HTTP 状态码仍用于表达网络和协议结果：

| HTTP | 含义 |
| --- | --- |
| 200 | 查询或更新成功。 |
| 201 | Session 创建成功。 |
| 202 | Message 接受成功。 |
| 400 | 请求格式或字段错误。 |
| 404 | Session/Message 不存在。 |
| 409 | 状态冲突。 |
| 422 | Skill 包或配置不兼容。 |
| 429 | 资源限制。 |
| 503 | 模型或依赖不可用。 |

## 3. 路径模型

### 3.1 物理路径与逻辑路径

```text
storage_root  = /mnt/nas
user_rel_path = T001/users/U001

user_root = /mnt/nas/T001/users/U001
```

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `storage_root` | absolute string | 是 | Runtime 使用的稳定挂载根路径。 |
| `user_rel_path` | relative string | 是 | 用户逻辑路径。 |
| `project_ref` | string 或 null | 否 | 项目逻辑引用。 |
| `project_rel_path` | relative string 或 null | 否 | 相对用户根目录的项目路径。 |

`storage_root` 语义：

- 它是稳定的逻辑挂载点，例如始终使用 `/mnt/nas`。
- NAS 设备迁移时，优先把新存储挂载到同一个 `storage_root`，逻辑身份不变。
- Runtime 内部以 `scope_id/project_ref` 定位 Workspace，`storage_root/user_path/project_path`
  是该 Workspace 的绝对路径绑定。
- `storage_root` 只在 `create-session` 由 Platform 提供并写入绑定；创建后所有流程只凭
  `session_id` 读取该绑定，不再需要传 `storage_root`。
- 同一用户 Project 再次 `create-session` 时，Runtime 以本次传入的路径为准刷新绑定；
  历史数据迁移（含已有文件的根路径调整）由 Platform 侧 DML 负责，Runtime 不提供接口
  改路径。

默认目录：

```text
workspace_root = <user_root>/workspace
default_skills = <user_root>/config/skills
```

项目目录：

| 传入 | 实际项目目录 |
| --- | --- |
| 只传 `project_ref` | `<user_root>/workspace/projects/<project_ref>` |
| 只传 `project_rel_path` | `<user_root>/<project_rel_path>` |
| 两者都传 | 使用 `project_rel_path`；`project_ref` 只作为稳定逻辑身份 |
| 都不传 | `<user_root>/workspace` |

### 3.2 NAS 迁移

推荐通过重新挂载保持相同 `storage_root`，逻辑身份不变。

如果必须修改根路径：

- 已有数据（`workspaces.storage_root/user_path/project_path` 及文件本体）由 Platform 侧
  DML 迁移，Runtime 不提供改路径接口。
- 迁移后的新会话直接在新根上 `create-session`，Runtime 以传入路径刷新绑定。
- 已开始的 Message 使用接受时冻结的解析路径。

后续 `send-message`、`list-session-files`、`read-session-file` 不接受 `storage_root`，
只从 `session_id` 绑定的根解析路径。

### 3.3 Skill 路径

`skill_paths` 的元素有两种合法形式：

1. 直接指向包含 `SKILL.md` 的 Skill 包目录。
2. 指向 Skill 根目录；Runtime 只扫描它的直接子目录，寻找每个子目录中的 `SKILL.md`。

不会递归扫描更深层目录。

示例：

```text
<user_root>/config/skills/
├── analysis/
│   └── SKILL.md
├── report/
│   └── SKILL.md
└── helper/
    └── README.md
```

传入：

```json
{
  "skill_paths": ["T001/users/U001/config/skills"]
}
```

Runtime 会发现 `analysis` 和 `report` 两个 Skill 包，忽略 `helper`。

规则：

- 不传 `skill_paths`：默认使用 `<user_root>/config/skills`，同样只扫描一层。
- 传入多个路径：按顺序处理。
- 空数组：不使用默认 Skill。
- Platform 不传 Skill 名称和版本；Runtime 从各包的 `SKILL.md` 读取。

## 4. 公开对象

### 4.1 Session

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `session_id` | UUID | Session ID。 |
| `title` | string | 会话标题。 |
| `user_rel_path` | string | 用户逻辑路径。 |
| `project_ref` | string 或 null | 项目逻辑引用。 |
| `project_rel_path` | string | 项目相对路径。 |
| `date_created` | date-time | 创建时间。 |
| `date_updated` | date-time | 最后修改时间。 |

### 4.2 Message

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `message_id` | UUID | Runtime 生成的 Message ID。 |
| `session_id` | UUID | 所属 Session。 |
| `message_seq` | integer | Session 内消息序号，从 1 开始。 |
| `input` | string | 用户输入。 |
| `status` | MessageStatus | 当前状态。 |
| `task_outcome` | string 或 null | `completed/partial/blocked`。 |
| `output` | string 或 null | 最终输出。 |
| `error` | Error 或 null | 终态错误。 |
| `date_created` | date-time | 创建时间。 |
| `date_updated` | date-time | 最后修改时间。 |

### 4.3 MessageStatus

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 已接受，等待执行。 |
| `RUNNING` | 正在执行。 |
| `WAITING_USER` | 等待用户输入。 |
| `WAITING_EXTERNAL` | 等待外部依赖。 |
| `RECOVERING` | 正在核验恢复。 |
| `CANCELLING` | 已接受终止请求，正在停止并核验已有操作。 |
| `SUCCEEDED` | 正常结束。 |
| `FAILED` | 失败。 |
| `CANCELLED` | 已按终止请求结束。 |
| `TIMED_OUT` | 已超时。 |

### 4.4 Error

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `code` | string | 错误码。 |
| `message` | string | 错误说明。 |
| `retryable` | boolean | 是否建议有界重试。 |
| `request_id` | string | 诊断关联 ID。 |

## 5. 核心接口

### 5.1 `POST /v1/create-session`

作用：创建 Session，绑定 NAS 根路径和用户逻辑路径。

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `storage_root` | absolute string | 是 | 当前物理根。 |
| `user_rel_path` | relative string | 是 | 用户逻辑路径。 |
| `project_ref` | string 或 null | 否 | 项目逻辑引用。 |
| `project_rel_path` | relative string 或 null | 否 | 项目相对路径。 |

请求示例：

```json
{
  "storage_root": "/mnt/nas",
  "user_rel_path": "T001/users/U001",
  "project_ref": "P001"
}
```

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "session_id": "11111111-1111-4111-8111-111111111111",
    "title": "新会话",
    "user_rel_path": "T001/users/U001",
    "project_ref": "P001",
    "project_rel_path": "workspace/projects/P001",
    "date_created": "2026-09-14T10:00:00Z",
    "date_updated": "2026-09-14T10:00:00Z"
  }
}
```

### 5.2 `POST /v1/update-session`

作用：修改会话标题。

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |
| `title` | string，1-200 | 是 | 新标题。 |

请求示例：

```json
{
  "session_id": "11111111-1111-4111-8111-111111111111",
  "title": "九月销售分析"
}
```

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "session_id": "11111111-1111-4111-8111-111111111111",
    "title": "九月销售分析",
    "user_rel_path": "T001/users/U001",
    "project_ref": "P001",
    "project_rel_path": "workspace/projects/P001",
    "date_created": "2026-09-14T10:00:00Z",
    "date_updated": "2026-09-14T10:05:00Z"
  }
}
```

### 5.3 `GET /v1/get-session`

作用：读取一个 Session。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "session_id": "11111111-1111-4111-8111-111111111111",
    "title": "九月销售分析",
    "user_rel_path": "T001/users/U001",
    "project_ref": "P001",
    "project_rel_path": "workspace/projects/P001",
    "date_created": "2026-09-14T10:00:00Z",
    "date_updated": "2026-09-14T10:05:00Z"
  }
}
```

### 5.4 `GET /v1/list-sessions`

作用：分页列出 Session。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `user_rel_path` | string | 否 | 按用户逻辑路径过滤。 |
| `project_ref` | string | 否 | 按项目过滤。 |
| `cursor` | string | 否 | 分页游标。 |
| `limit` | integer | 否 | 默认 50，最大 100。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "items": [
      {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "title": "九月销售分析",
        "user_rel_path": "T001/users/U001",
        "project_ref": "P001",
        "project_rel_path": "workspace/projects/P001",
        "date_created": "2026-09-14T10:00:00Z",
        "date_updated": "2026-09-14T10:05:00Z"
      }
    ],
    "next_cursor": null
  }
}
```

### 5.5 `POST /v1/send-message`

作用：向 Session 提交一条用户消息。Runtime 负责生成 `message_id` 并执行。

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | 目标 Session。 |
| `input` | string，1-100000 | 是 | 用户输入原文。 |
| `agent_ref` | string | 否 | Agent 配置引用。 |
| `skill_paths` | string[] 或 null | 否 | Skill 包目录或一层 Skill 根目录。 |

路径来自 `session_id` 已绑定的 `storage_root`，请求不再接受 `storage_root`。

执行语义：

1. Runtime 接受后自行生成 `message_id`。
2. Runtime 首期只执行当前 Session 的一条 Message。
3. 如果 Session 已有未结束的 Message，返回业务码 `1005` 和 HTTP 409，不创建新 Message。
4. 是否排队、等待还是改变对话方向由 Platform 决定。
5. 正在执行的 Message 通过 `cancel-message` 显式终止；已有未结束 Message 时仍不能提交下一条 Message。
6. 网络超时后不要盲目重发；先调用 `get-session-state` 或 `list-session-messages`。

该设计代价是：Runtime 不承诺网络超时场景下的 exactly-once。Platform 关闭创建消息的自动
重试；若无法确认结果，应以 Session 当前状态为准继续展示和恢复。

请求示例：

```json
{
  "session_id": "11111111-1111-4111-8111-111111111111",
  "input": "分析销售数据并生成报告",
  "agent_ref": "general@1",
  "skill_paths": [
    "T001/users/U001/config/skills"
  ]
}
```

响应类型：`text/event-stream`。

首个 SSE 事件必须返回 Message 接受结果：

```text
id: 11111111-1111-4111-8111-111111111111:1
event: message.accepted
data: {"code":"0000","message":"success","data":{"event_id":"44444444-4444-4444-8444-444444444444","session_id":"11111111-1111-4111-8111-111111111111","message_id":"22222222-2222-4222-8222-222222222222","seq":1,"type":"message.accepted","occurred_at":"2026-09-14T10:00:00Z","data":{"message_id":"22222222-2222-4222-8222-222222222222","status":"QUEUED"}}}
```

后续 SSE 事件在同一连接中继续发送 `message.*`、`tool.*`、`skill.*` 和
`workspace.committed`，直到 `message.finished`。

如果 Message 因 Session busy 未创建，则不建立 SSE，直接返回普通 JSON 错误：

```json
{
  "code": "1005",
  "message": "session already has a running message",
  "data": {}
}
```

### 5.6 `GET /v1/get-session-state`

作用：读取 Session 当前执行状态、待回应项和事件游标。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "session": {
      "session_id": "11111111-1111-4111-8111-111111111111",
      "title": "九月销售分析",
      "user_rel_path": "T001/users/U001",
      "project_ref": "P001",
      "project_rel_path": "workspace/projects/P001",
      "date_created": "2026-09-14T10:00:00Z",
      "date_updated": "2026-09-14T10:05:00Z"
    },
    "current_message": {
      "message_id": "22222222-2222-4222-8222-222222222222",
      "status": "WAITING_USER"
    },
    "pending_reply": {
      "pending_id": "33333333-3333-4333-8333-333333333333",
      "message_id": "22222222-2222-4222-8222-222222222222",
      "kind": "clarification",
      "prompt": "请选择统计时间范围"
    },
    "event_cursor": "11111111-1111-4111-8111-111111111111:12"
  }
}
```

Platform 从这里取得 `data.pending_reply.pending_id`，再调用 `reply`。SSE 的
`message.waiting` 事件也会携带同一个 `pending_id`。

### 5.7 `GET /v1/get-message`

作用：读取一条 Message。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `message_id` | UUID | 是 | Message ID。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "message_id": "22222222-2222-4222-8222-222222222222",
    "session_id": "11111111-1111-4111-8111-111111111111",
    "message_seq": 1,
    "input": "分析销售数据并生成报告",
    "status": "SUCCEEDED",
    "task_outcome": "completed",
    "output": "报告已生成。",
    "error": null,
    "date_created": "2026-09-14T10:00:00Z",
    "date_updated": "2026-09-14T10:05:00Z"
  }
}
```

### 5.8 `GET /v1/list-session-messages`

作用：分页读取 Session 的消息历史。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |
| `cursor` | string | 否 | 分页游标。 |
| `limit` | integer | 否 | 默认 50，最大 100。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "items": [
      {
        "message_id": "22222222-2222-4222-8222-222222222222",
        "message_seq": 1,
        "input": "分析销售数据并生成报告",
        "status": "SUCCEEDED",
        "task_outcome": "completed",
        "output": "报告已生成。"
      }
    ],
    "next_cursor": null
  }
}
```

### 5.9 `POST /v1/reply`

作用：回应当前 `WAITING_USER` Message。

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `message_id` | UUID | 是 | 等待回应的 Message。 |
| `pending_id` | UUID | 是 | 待回应项 ID。 |
| `text` | string，1-100000 | 是 | 用户回应。 |

请求示例：

```json
{
  "message_id": "22222222-2222-4222-8222-222222222222",
  "pending_id": "33333333-3333-4333-8333-333333333333",
  "text": "统计最近 30 天"
}
```

响应类型：`text/event-stream`。

首个事件为 `message.resumed`：

```text
id: 11111111-1111-4111-8111-111111111111:13
event: message.resumed
data: {"code":"0000","message":"success","data":{"event_id":"55555555-5555-4555-8555-555555555555","session_id":"11111111-1111-4111-8111-111111111111","message_id":"22222222-2222-4222-8222-222222222222","seq":13,"type":"message.resumed","occurred_at":"2026-09-14T10:06:00Z","data":{"message_id":"22222222-2222-4222-8222-222222222222","status":"QUEUED"}}}
```

后续事件继续通过同一个 SSE 连接返回。

同一个 `pending_id` 重复提交不会再次消费；已解决的 `pending_id` 不再接受新内容。

### 5.10 `POST /v1/cancel-message`

作用：终止一条尚未结束的 Message。

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `message_id` | UUID | 是 | 要终止的 Message。 |

请求示例：

```json
{
  "message_id": "22222222-2222-4222-8222-222222222222"
}
```

响应类型：`application/json`。返回终止请求处理后的当前 Message。

`QUEUED` Message 可以直接返回 `CANCELLED`；正在执行或等待中的 Message 通常先返回
`CANCELLING`。此时仅表示 Runtime 已接受终止请求，不表示 Python、工具或远端副作用
已经停止。Platform 必须继续等待原 SSE 或 `stream-session-events` 收到该
`message_id` 的 `message.finished`，再以其中的 `CANCELLED` 状态作为终态。

终止请求幂等：已经处于 `CANCELLING` 或终态的 Message 返回当前状态，不重复写入审计
或事件。终止不会撤销已经提交的文件，也不会撤回已经发生的远端副作用。

## 6. 事件接口

### 6.1 `GET /v1/stream-session-events`

作用：页面刷新或网络断开后的恢复接口。普通发送流程使用 `send-message` 自带的 SSE，
不需要额外调用本接口。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |
| `after_seq` | integer | 否 | 只发送该序号之后的事件。 |

SSE 示例：

```text
id: 11111111-1111-4111-8111-111111111111:12
event: message.completed
data: {"code":"0000","message":"success","data":{"event_id":"44444444-4444-4444-8444-444444444444","session_id":"11111111-1111-4111-8111-111111111111","message_id":"22222222-2222-4222-8222-222222222222","seq":12,"type":"message.completed","occurred_at":"2026-09-14T10:05:00Z","data":{"message_id":"22222222-2222-4222-8222-222222222222","text":"报告已生成。"}}}
```

规则：

- Platform 按 `session_id + seq` 去重。
- `message.delta` 是增量。
- `message.completed` 是完整文本。
- `message.finished` 是 Message 终态。
- 断线只影响订阅连接，不改变 Message 状态。

事件类型：

| 类型 | 含义 |
| --- | --- |
| `message.accepted` | Message 已接受。 |
| `message.started` | 开始处理。 |
| `message.waiting` | 等待用户或外部依赖。 |
| `message.resumed` | 用户回应后恢复。 |
| `message.recovering` | 正在核验恢复。 |
| `message.cancel_requested` | 已接受终止请求，进入 `CANCELLING`。 |
| `message.finished` | Message 终态。 |
| `message.delta` | 模型文本增量。 |
| `message.completed` | 完整模型回复。 |
| `tool.prepared` | 工具意图已记录，`data.input` 携带实际代码或命令。 |
| `tool.started` | 工具开始。 |
| `tool.output` | 工具输出分片，执行过程中持续产生；`data.stream` 区分 stdout/stderr。 |
| `tool.finished` | 工具结束，携带状态与错误；不再暴露主机文件路径。 |
| `tool.unknown` | 工具结果未知。 |
| `skill.activated` | Skill 已加载。 |
| `skill.started` | Skill 操作开始。 |
| `skill.finished` | Skill 操作结束。 |
| `workspace.committed` | 文件修订已提交。 |

`message.waiting` 的 `data` 结构：

```json
{
  "pending_reply": {
    "pending_id": "33333333-3333-4333-8333-333333333333",
    "message_id": "22222222-2222-4222-8222-222222222222",
    "kind": "clarification",
    "prompt": "请选择统计时间范围"
  }
}
```

### 6.2 `GET /v1/list-session-events`

作用：分页读取 Session 事件，用于刷新和诊断。

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |
| `after_seq` | integer | 否 | 从该序号之后读取。 |
| `limit` | integer | 否 | 默认 200，最大 1000。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "items": [],
    "next_after_seq": 12
  }
}
```

## 7. 诊断接口

### 7.1 `GET /health`

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "status": "ok",
    "api_version": "1",
    "model_configured": true,
    "execution_backend": "local_process",
    "permission_mode": "default_allow",
    "isolation_mode": "trusted_logical"
  }
}
```

### 7.2 `GET /v1/list-session-files`

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "items": [
      {
        "path": "report.md",
        "size_bytes": 2048,
        "digest": "sha256:..."
      }
    ],
    "next_cursor": null
  }
}
```

### 7.3 `GET /v1/read-session-file`

查询参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `session_id` | UUID | 是 | Session ID。 |
| `path` | relative string | 是 | 项目内相对路径。 |

返回示例：

```json
{
  "code": "0000",
  "message": "success",
  "data": {
    "path": "report.md",
    "content": "# Report\n",
    "digest": "sha256:...",
    "truncated": false
  }
}
```

## 8. Platform 侧需要保存

| 数据 | 用途 |
| --- | --- |
| `tenant_id/user_id` | Platform 自己的权限和业务关系。 |
| `user_rel_path` | 稳定的用户逻辑身份。 |
| `project_ref/project_rel_path` | 项目定位。 |
| `session_id` | Session 句柄。 |
| `message_id` | Runtime 返回的消息句柄。 |
| `pending_id` | 澄清或核验项。 |
| `event_cursor` | SSE 断线恢复。 |

Platform 不需要传：

- 用户角色或 ACL；
- 登录令牌；
- `tenant_ref` 或 `user_ref`；
- `scope_id`；
- 客户端生成的 `message_id`；
- `Idempotency-Key`。

## 9. 实现落地范围

确认本文后，需要同步修改：

1. 核心 contracts 和 ports，把公开 Message 映射到内部 Run。
2. Runtime HTTP transport，使用动作型接口名。
3. 所有 HTTP 响应统一包装为 `{code,message,data}`。
4. SQLite/PostgreSQL 会话、Workspace 和 Message 映射。
5. Session 级事件序号与 SSE 投影，不向 Platform 暴露 `run_id`。
6. Skill resolver 支持直接包目录和一层根目录扫描。
7. OpenAPI 和测试。
