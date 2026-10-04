# Impala 工具接入

日期：2026-10-04。当前实现为原生 Agent 工具，复用 Local/LangGraph Harness、工具账本和现有 `tool.*` 事件；没有新建 MCP 服务或公开业务 API。真实 Impala、LDAP 和 Ranger 尚未验证。

## 1. 已确认的身份模型

目前 A 部门共用账号 1，采用 LDAP 用户名/密码。Runtime 管理部门账号绑定，Impala/Ranger 管理该账号的数据库权限。模型参数不允许包含部门、账号、凭据、连接地址或查询选项。

- `trusted_single_department`：服务端统一绑定 A，配置必须恰好包含一个部门。适用于当前可信内部使用。
- `mapped_departments`：服务端配置 `scope_departments`，由 Runtime 的 scope 定位部门，再选择账号；未知 scope 不回退 A。示例预留 A/账号 1、B/账号 2。

当前 Platform 没有真实认证，存储路径产生的 scope 不能作为生产身份认证。多部门配置提供的是绑定扩展能力，开放给不可信用户之前必须由认证边界产生可信 scope，并对现有 Session/Message/文件 HTTP 接口落实归属检查。默认允许仍仅用于可信验证，当前不新增部门管理后台。

部门配置在每次解析时读取。接受 Message 时，在现有 `RunSnapshot.tool_refs` 中保存六个工具名及 `impala-binding@<digest>`；摘要只包含非秘密连接/策略配置和凭据引用名称。执行时重新解析并核对摘要；变更后拒绝旧 Message 派发，运行中的查询在下一检查点停止。新 Message 使用新配置。同键接受复用旧快照。

LDAP 凭据从 Runtime 环境读取；使用 `use_ssl=True`、`verify_cert=True`，自签/企业 CA 使用 `ca_cert`。启用前需要集群提供可验证的 TLS 连接。驱动不透明重试；认证后先查询 `effective_user()`，与配置账号不符则不执行业务 SQL。

首版针对已确认的 LDAP，采用二进制 HiveServer2+TLS。拒绝无认证、HTTP、JWT 和 Kerberos 配置。Impyla 0.23.0 的 retries 参数包含首次尝试，因此设为 1 才是单次执行；HTTP 路径不落实 connect 的 timeout，必须另做验证过的传输适配后才可启用。驱动原始 RPC/异常日志可能包含句柄或秘密，适配器关闭这两个库日志源，保留经过清理的工具执行证据。

连接每次查询独立创建、由一个线程独占并关闭，没有共享连接池。用户身份、Session 与部门账号保存在查询证据中；同部门也不能查询或取消其他用户/Session 的句柄。数据库审计看到部门共享账号，具体用户通过 Code Forge 的 scope、Session/Message、工具操作和 query ID 关联；当前 scope 的可信程度取决于上层认证。

## 2. 工具

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `impala_search_tables` | 可选 database、pattern | 不提供 database 时 SHOW DATABASES；提供时 SHOW TABLES，可按模式查找 |
| `impala_describe_table` | table、可选 database | DESCRIBE 返回的列/类型/注释及分区元数据 |
| `impala_explain` | sql | 单条受限 SELECT 的 EXPLAIN 文本行；不执行 SELECT |
| `impala_query` | sql、可选 max_rows | 执行并等待有界结果，包含 query_ref、query_id、状态、列、行、行数、截断原因、耗时、部门和账号 |
| `impala_query_status` | query_ref | 本 Session 拥有的操作状态摘要，不重新输出数据行 |
| `impala_cancel_query` | query_ref | 请求取消；返回 CANCELLING 表示尚待远端确认，已结束查询返回其原状态 |

`impala_query` 是有截止时间的等待式工具。当前模型工具列表串行派发，模型通常在查询结束后调用状态工具；进行中的查询通过 Message 的取消接口取消，SSE 进度提供 query_ref。状态工具读适配器持久记录，不查询整个集群。工具数据、表注释都作为数据而非指令处理。

## 3. 安全和资源边界

使用 SQLGlot 28.0.0 的 Hive AST 作为保守语法子集，只接受单条 SELECT/只读集合查询；拒绝嵌套写入、控制语句、SELECT INTO、外部表函数、查询 hint、非字面量 LIMIT 和未批准的匿名 UDF。不认识的语法直接拒绝；解析成功不证明语法、权限或执行成本正确，最终由 Impala 校验。输出 SQL 删除注释，解析失败不把原 SQL/驱动异常写入普通日志。

默认限制：

| 范围 | 限制 |
| --- | --- |
| 单次 SQL | 32,000 UTF-8 字节 |
| 单次结果 | 默认 100 行，最大 1,000 行；完整 JSON 32,000 字节；单元格 2,048 字节；最多 100 列 |
| 单次查询 | 总截止 60 秒、单 RPC 超时 5 秒；检查点和远端清理可能额外花费有界 RPC 时间 |
| Impala 选项 | EXEC_TIME_LIMIT_S、MEM_LIMIT=1024m、SCAN_BYTES_LIMIT=10240m、NUM_ROWS_PRODUCED_LIMIT、MAX_ROW_SIZE=64k、FETCH_ROWS_TIMEOUT_MS=1000；可配置 REQUEST_POOL |
| 单 Message | 12 次查询、累计 5,000 行、256,000 字节 |
| 每用户每小时 | 120 次查询、20,000 行、1,024,000 字节 |
| 每部门每小时 | 600 次查询、100,000 行、5,120,000 字节 |
| 同时执行 | 每用户 1 个、每部门 4 个 |

所有元数据、计划和查询都计入预算；账号校验 SQL 属于每次查询内部步骤。小时预算按 Unix 时间的固定整点窗口计数，跨新 Message 不重置。单副本线程锁配合落盘计数；不证明多副本全局限流。派发先预留结果额度，确定结果落盘后按实际行数和完整 JSON 字节结算；崩溃保留预留值，不能靠重启重置预算。并发拒绝直接返回错误，不创建无限等待队列。

SQL 在最外层设置不超过 max_rows+1 的 LIMIT，保留更小的原始 LIMIT；多取一行仅用于识别行数截断。驱动 buffersize/arraysize/fetchmany 都为 1，不调用 fetchall。达到返回行数或字节限额即停止获取并关闭操作；单元格截断和结果截断均明确标记。异常/取消时丢弃尚未确认的部分数据。即使一行很宽，也要先由 HS2 解码才能在客户端截断，MAX_ROW_SIZE 和结果列数保护不能冒充传输层严格包大小限制。

大表可配置 `required_filters`，例如 `{"analytics.sales":"dt"}`。检查真实表扫描所在 SELECT 的 WHERE：需要该列的常量比较，拒绝 dt=dt、OR 1=1、其他别名过滤和无过滤的分支。它只证明存在受支持的过滤形式，不能证明过滤选择性；LIMIT 也不限制全表聚合成本。扫描选项的存储覆盖、超额行为和资源池配置必须按实际 Impala 版本验证，不支持选项时明确失败，不静默取消保护。

环境变量不传给现有模型 Python/Bash 子进程（已有环境白名单）。不过同 OS 用户仍可能读取宿主配置、环境或其他用户文件，这不是沙箱。当前只适合可信内部环境；生产强隔离要由独立身份/执行后端和受保护凭据服务完成，不把本次逻辑绑定描述为生产隔离。

## 4. 账本、取消与故障

工具先 `prepare_tool` 再派发，使用 Attempt+模型 tool_call_id 的稳定逻辑槽；同槽同参数复用已保存结果，不同参数冲突。SQL 参数写 `input.txt`；绑定用户模式的 `result.json`、`stdout.log` 在现有 User Root/tool-output 中，legacy 在 Runtime/operations。现有工具事件可在 Platform 内联显示，无新事件字段。

适配器另在 Runtime/impala/queries 下保存 scope、Session、内部执行关联、部门、账号、绑定摘要与有界结果；Runtime/impala/budgets 保存预算。只有非秘密 query ID 持久化；HS2 操作句柄包含秘密，不进入模型或磁盘。文件先写临时文件、fsync 后替换；不同查询独立连接，进度、结果和错误都复用 tool.output/tool.finished/tool.unknown。

用户取消、查询截止、绑定停用或 Worker 关停会通知拥有连接的线程，发送 CancelOperation 并读取状态，再关闭操作和连接。确认远端终态后才返回 CANCELLED；确认 Message 取消后结束 Message。无法确认则工具 UNKNOWN，不声称取消成功，也不自动重发 SQL。

进程重启后 RUNNING 的本地查询记录返回 UNKNOWN；不能仅凭 query ID 恢复带秘密的 HS2 句柄。需要操作者通过 Impala 运维渠道核验/取消，随后发起新 Message。当前没有远端故障自动恢复服务、MCP 取消适配或多副本故障接管。普通 UNKNOWN 停止当前 Agent 循环；LangGraph 报执行失败，Local 返回 blocked，均不报告查询成功。Message 已在 CANCELLING 时远端不明，保留该状态供人工核验。

## 5. 配置与使用

安装固定依赖：

```sh
.venv/bin/python -m pip install -r requirements-impala.lock
mkdir -p .runtime/config
cp config/examples/impala.single-department.json .runtime/config/impala.json
```

编辑 `.runtime/config/impala.json`，填写实际 host、port、user（账号 1）、database、企业 CA 路径和已配置的资源池。JSON 只包含 password_env 引用，不写密码。`.runtime` 和 `.env` 已忽略。

在忽略的 `.env` 或部署密钥环境中设置：

```dotenv
FORGE_IMPALA_CONFIG=config/impala.json
FORGE_IMPALA_A_PASSWORD=实际密码
```

配置路径相对 Runtime Root（默认 .runtime），也可使用绝对路径。重启本地 Runtime 后，新 Message 接受时启用工具；旧 Message 不追加新工具。已有模型运行入口不变。可以直接输入“查找销售相关表，按地区统计上月销售额”，观察工具轨迹，再让 execute_python 对汇总结果生成图表。

`FORGE_IMPALA_CONFIG` 未配置时不注册 Impala 工具，不影响原 Python/Bash。缺少驱动或配置损坏时明确 DEPENDENCY_UNAVAILABLE。没有绑定的 scope 不注册工具。启用配置但未提供密码时查询明确失败。当前没有真实配置/凭据，不默认启用示例或尝试连接。

### 5.1 部门绑定配置的中文释义

连接配置决定“哪个部门使用哪个账号连接哪里”，`policy` 决定“该账号通过 Agent 可以怎样查询”。配置示例见 [A 部门](../config/examples/impala.single-department.json) 和 [多部门](../config/examples/impala.mapped-departments.json)。

| 英文字段 | 中文名称 | 详细解释 |
| --- | --- | --- |
| `mode` | 部门绑定模式 | 当前使用 `trusted_single_department`，所有使用者统一使用一个部门的账号，且只允许配置一个部门。以后可使用 `mapped_departments`，按 Runtime scope 的部门映射选择账号。 |
| `default_department` | 默认部门 | 单部门模式当前填 `A`，表示选择 `departments.A`；多部门模式不使用它作为未知用户的兜底。 |
| `departments` | 部门配置集合 | 每个部门分别保存连接信息、凭据引用和查询策略。当前 A 对应共享账号 1，以后 B 可以对应共享账号 2。 |
| `scope_departments` | 用户与部门映射 | 多部门模式使用，例如某个 Runtime scope 对应 A、另一个对应 B。没有映射的 scope 不启用工具。映射依赖上层提供可信身份，不能让模型或浏览器随意指定。 |
| `enabled` | 是否启用部门绑定 | `true` 启用，`false` 停用；省略时默认启用。停用后禁止新查询，正在执行的查询会在下一检查点尝试取消。 |
| `policy` | 查询限制策略 | 本部门的行数、字节、时间、资源、并发、累计额度和 SQL 过滤要求。省略的策略字段使用代码默认值。 |

`A`、`B` 是配置中的部门标识；实际数据库用户名填写在 `user` 中。当前实现不维护一套独立的库/表/列授权规则，实际数据权限由该部门账号在 Impala/Ranger 中的权限决定。

### 5.2 连接与认证配置的中文释义

| 英文字段 | 默认值或示例 | 中文解释 |
| --- | --- | --- |
| `host` | 必填 | Impala 连接地址，填写实际域名或 IP。示例的 `impala.example.invalid` 不能实际连接。 |
| `port` | `21050` | HiveServer2 连接端口，必须与集群提供的端口一致；可配置范围 1—65535。 |
| `database` | `"default"` | 默认数据库。SQL 使用 `FROM sales` 时从此库查找；使用 `FROM analytics.sales` 时采用明确指定的库。当前只接受普通数据库标识符。 |
| `auth_mechanism` | `"LDAP"` | 认证方式。当前实现只接受 LDAP 用户名/密码，不支持通过配置切换为无认证、JWT 或 Kerberos。 |
| `user` | 必填 | 本部门实际共享的 Impala 用户名。A 部门应填账号 1 的真实用户名；认证后工具会检查 `effective_user()` 是否与该配置一致。 |
| `password_env` | `"FORGE_IMPALA_A_PASSWORD"` | 保存密码的环境变量名称，填写变量名而非密码内容。实际密码放在服务环境或忽略的 `.env` 文件中。变量缺失或为空时明确失败。 |
| `ca_cert` | `""` | 服务端证书的 CA 文件路径。留空使用系统信任的 CA；企业内部 CA 可填写证书的绝对路径。始终启用 TLS 和证书校验。 |
| `request_pool` | `""` | Impala 查询资源池名称。填写后提交到指定池；留空时不设置 REQUEST_POOL，由集群默认路由处理。需要由集群管理员预先配置，不能只填一个新名字就创建资源池。 |
| `use_http_transport` | `false` | 是否使用 HTTP 传输。当前版本只允许 `false`，设为 `true` 会拒绝配置，避免驱动忽略 RPC 超时。 |
| `http_path` | `""` | 为未来 HTTP 适配预留的路径；当前不启用 HTTP，保持空值即可。 |

服务启动相关环境变量：

| 英文变量 | 中文解释 |
| --- | --- |
| `FORGE_IMPALA_CONFIG` | 指定部门配置 JSON 路径；相对路径以 Runtime Root 为基准，默认 Root 为 `.runtime`。例如 `config/impala.json` 对应 `.runtime/config/impala.json`。未设置时不注册 Impala 工具。 |
| `FORGE_IMPALA_A_PASSWORD` | A 部门账号 1 的实际密码。变量名称由该部门的 `password_env` 引用，不是固定只能叫这个名字。B 部门可使用另一个独立变量。 |

### 5.3 单次结果限制的中文释义

| 英文字段 | 默认值 | 中文解释及超限行为 |
| --- | --- | --- |
| `default_rows` | 100 行 | Agent 调用 `impala_query` 时未提供工具参数 `max_rows`，采用此返回行数上限。仅在 SQL 中写 LIMIT 1000，不会自动把默认返回上限提高到 1000。 |
| `max_rows` | 1000 行 | 部门策略中单次调用允许申请的最大行数。Agent 可以申请更少的行数；申请超过此值直接拒绝。服务端配置当前最多可设为 10000。 |
| `max_bytes` | 32000 字节 | 单次完整结果 JSON 的字节上限，包括列名、数据、状态等。工具会预留最终元数据空间，接近上限时停止增加行并标记 `byte_limit`，因此可能不足行数上限。 |
| `cell_bytes` | 2048 字节 | 单个单元格文本的字节上限。超长日志、JSON 或其他文本会截断，并标记 `cell_limit`；其他数据行仍受总结果额度限制。 |
| `max_columns` | 100 列 | 最多允许的结果列数。超过时工具失败，不会悄悄删除后面的列；列元数据过大也会明确失败。当前最多可设为 100。 |

这里的 `_bytes` 按 UTF-8 编码后的字节数计算，不是字符数。普通汉字通常占 3 个字节，JSON 结构和转义也占空间。不同限制同时生效：例如申请 1000 行，但约 80 行就接近字节上限，实际会返回约 80 行并标记字节截断。

**超过 1000 行的实际处理示例**：部门 `max_rows=1000`，且本次工具调用明确申请 `max_rows=1000`，SQL 原本会返回更多行时：

1. 最外层 SQL LIMIT 被限制到最多 1001 行；保留原 SQL 中更小的 LIMIT。
2. 最多返回前 1000 行，额外一行只用来判断是否截断，不会交给模型。
3. 停止读取并关闭查询操作，不自动分页拉取剩余数据，不统计剩余总行数。
4. 如果字节上限等其他限制未先触发且远端关闭正常，返回下列状态。查询执行成功与结果完整性通过不同字段表达。

```json
{
  "status": "SUCCEEDED",
  "row_count": 1000,
  "truncated": true,
  "truncation_reasons": ["row_limit"]
}
```

若调用未提供 `max_rows`，默认使用 100 行，对应最多读取 101 行用于判断行数截断。若原 SQL 明确 LIMIT 1000，恰好返回 1000 行，则工具没有证据判断这条 SQL 之外还存在多少行，不会声称知道原表总行数。任何结果截断都不意味着聚合统计完整，Agent 应根据标记重新过滤或聚合。

### 5.4 时间、资源和并发限制的中文释义

| 英文字段 | 默认值 | 中文解释 |
| --- | --- | --- |
| `timeout_seconds` | 60 秒 | 单次工具查询的整体截止时间，覆盖连接、账号校验、执行和读取结果；超时后尝试取消。检查点及远端取消/清理可能额外花费有界 RPC 时间。当前最多可设为 3600 秒。 |
| `rpc_timeout_seconds` | 5 秒 | 一次网络请求的超时，如提交 SQL、查询状态、获取结果。它不是整个 SQL 的总时限，且不能大于 `timeout_seconds`。 |
| `mem_limit_mb` | 1024 MB | 传给 Impala 的 MEM_LIMIT，限制单个查询在每个 Impala 节点上的内存。不是 Agent 本地内存，也不是整个集群合计只用 1 GB；查询使用多个节点时合计内存可能更大。实际执行还受集群资源池限制。 |
| `scan_bytes_limit_mb` | 10240 MB | 传给 Impala 的 SCAN_BYTES_LIMIT，限制查询扫描数据量。扫描量与最终返回量不同，一行 SUM 结果也可能扫描大量数据。实际存储覆盖、超额检查行为和选项支持需按集群版本验证。 |
| `max_user_concurrency` | 1 个 | 一个 Runtime 用户 scope 最多同时执行多少个 Impala 查询，即使该用户有多个 Session 也合计计算。 |
| `max_department_concurrency` | 4 个 | 本部门所有用户合计最多同时执行多少个查询；共享账号的压力按部门汇总。 |

达到并发上限时直接拒绝新查询，不创建无限排队。配置 4 个并发只设定上限，不会主动让当前串行 Worker 或模型工具列表产生 4 路任务。当前并发计数是单副本本地保护，不代表多副本的集群全局限流。

内存语义见 [Impala MEM_LIMIT](https://impala.apache.org/docs/build/html/topics/impala_mem_limit.html)，扫描语义见 [SCAN_BYTES_LIMIT](https://impala.apache.org/docs/build/html/topics/impala_scan_bytes_limit.html)，资源池语义见 [REQUEST_POOL](https://impala.apache.org/docs/build/html/topics/impala_request_pool.html)。

### 5.5 单任务与小时累计额度的中文释义

Message 表示用户提交的一次任务。Agent 为完成这次任务可能先找表、看结构、EXPLAIN，再查询多次；这些读取/计划调用共享该 Message 的预算。

| 英文字段 | 默认值 | 中文解释 |
| --- | --- | --- |
| `max_queries_per_message` | 12 次 | 一次用户任务最多派发多少次元数据、计划或数据查询。 |
| `max_rows_per_message` | 5000 行 | 该任务所有查询合计最多返回多少行。 |
| `max_bytes_per_message` | 256000 字节 | 该任务所有查询合计最多返回多少完整结果 JSON 字节。 |
| `max_queries_per_user_hour` | 120 次 | 一个用户每小时跨 Message 累计最多派发多少次查询。 |
| `max_rows_per_user_hour` | 20000 行 | 一个用户每小时跨 Message 累计最多获取多少行。 |
| `max_bytes_per_user_hour` | 1024000 字节 | 一个用户每小时跨 Message 累计最多获取多少结果 JSON 字节。 |
| `max_queries_per_department_hour` | 600 次 | 本部门所有用户每小时合计最多派发多少次查询。 |
| `max_rows_per_department_hour` | 100000 行 | 本部门所有用户每小时合计最多获取多少行。 |
| `max_bytes_per_department_hour` | 5120000 字节 | 本部门所有用户每小时合计最多获取多少结果 JSON 字节。 |

小时额度按固定整点窗口计算，例如北京时间 10:00—11:00 属于一个窗口，11:00 开始新窗口。新建 Message 不重置小时额度；重启 Runtime 不清空已保存的额度计数。

计数和超额行为：

- 找表、查看结构、EXPLAIN、执行 SQL 都计入上述预算；查询状态和取消操作不计入数据库查询预算。每次查询内部的 effective_user 校验不单独增加一次工具查询次数。
- 派发前先预留本次申请的行数和单次 `max_bytes` 字节额度，确定结果落盘后按实际结果结算。如果剩余额度不足以预留本次申请，即使实际可能只返回 10 行，也会拒绝查询；可在现有策略内申请更小的行数，但不能由模型提高额度。
- 已派发但失败的查询仍消耗一次查询次数；崩溃后未结算的预留值保留，不能靠重启重新获得额度。
- 任何一个 Message、用户小时、部门小时预算不足，都会拒绝新的查询；当前没有配额等待队列。

### 5.6 SQL 过滤要求、配置范围与生效方式

| 英文字段 | 默认值 | 中文解释 |
| --- | --- | --- |
| `required_filters` | `{}` | 指定某张表必须有某列的常量过滤条件。例如 `{"analytics.sales":"dt"}` 要求扫描销售表的 SELECT 中有 dt 的受支持 WHERE 比较。空对象表示不额外要求指定过滤列。检查不证明过滤选择性，扫描和时间限制仍然生效。 |
| `allowed_udfs` | `[]` | 允许的额外函数名列表。解析器无法识别为标准函数的匿名函数调用默认被拒绝，经过操作者确认的函数可以加入此名单；这不授予数据库函数执行权限，也不改变 Impala/Ranger 授权。 |

日期过滤示例：

```sql
-- 满足已配置的日期过滤形式。
SELECT region, SUM(amount)
FROM analytics.sales
WHERE dt >= '2026-10-01' AND dt < '2026-10-02'
GROUP BY region;

-- 配置 analytics.sales 必须过滤 dt 后，此查询会被拒绝。
SELECT * FROM analytics.sales;
```

数值策略必须为正整数，不能用 0 表示“不限制”。当前代码还校验：

```text
default_rows <= max_rows <= 10000
4096 <= max_bytes <= 256000
cell_bytes <= max_bytes
rpc_timeout_seconds <= timeout_seconds <= 3600
max_columns <= 100
```

当前固定在代码中的限制包括单次 SQL 32000 UTF-8 字节、MAX_ROW_SIZE=64k、FETCH_ROWS_TIMEOUT_MS=1000、驱动单批获取 1 行，以及只读语法/LDAP/TLS 约束；它们不是当前 JSON 可自由调整的字段。

修改已有 JSON 配置后，新 Message 使用新设置，旧 Message 检测到绑定摘要变化会停止继续派发，正在运行的查询在下一检查点尝试取消。首次设置 `FORGE_IMPALA_CONFIG` 或修改 `.env` 的密码需要重启 Runtime，以加载服务环境。上述策略保护仍限定于当前可信内部/单副本环境，不代表生产身份与 OS 隔离已经完成。

## 6. 验证分层

`tests/test_impala_tools.py` 使用确定性模型/HS2 替身，覆盖 TC-B23 参数/只读/身份、TC-B24 复用、TC-B25 参数冲突、TC-B27 确定失败、TC-B29 未知不重放、TC-B30 输出限额、TC-B35 取消、TC-B36 截止。SQLite/文件和 LangGraph 使用真实实现；替身不证明真实 LDAP/Ranger、Impala SQL 方言和资源选项、真实模型选工具、远端取消、生产隔离或 HA。

真实集群验收仍需要：账号 1 的可验证 TLS/LDAP 连接、effective_user 一致、允许/拒绝的表/列、超行/超字节/长查询、Message 取消和资源池行为。B 部门扩展时额外用两个真实账号验证权限差异与归属。不要在聊天或版本库提交密码。

依据：[Impyla](https://github.com/cloudera/impyla)、[Impala 授权](https://impala.apache.org/docs/build/html/topics/impala_authorization.html)、[查询选项](https://impala.apache.org/docs/build/html/topics/impala_query_options.html)、[SQLGlot AST](https://github.com/tobymao/sqlglot/blob/main/posts/ast_primer.md)。
