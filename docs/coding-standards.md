# 代码目录与设计规范

版本：0.4；日期：2026-09-10。适用于架构核心、后续 Runtime 适配器与最小 Platform。标注“约束”的内容不得自行变更；内部实现方式可在不破坏契约时选择。

## 1. 目录约定

以下 core/文档已存在；标为计划的目录由对应任务包创建，不要求现在铺空文件或占位应用。

```text
README.md                         # 入口、当前能力与验证边界
AGENTS.md                         # 所有实现智能体必须遵守的简明规则
requirements.md                   # 已确认范围
pyproject.toml                    # 核心包/开发工具配置
src/code_forge/
  contracts.py                    # 核心枚举、DTO、错误；标准库依赖
  ports.py                        # 存储/授权/执行/快照接口；不实现基础设施
  service.py                      # 任务接受的应用服务
  state_machine.py                # 纯状态转换与不变量
  audit.py                        # 审计字段的纯规则
  transport/                      # [计划 R5] HTTP/SSE/DTO映射/错误映射
  runtime/                        # [计划 R4] 调度、Worker、取消与恢复协调
  persistence/                    # [计划 R1] PG Repository、事务、fenced Checkpointer
  harness/                        # [计划 R3] AgentFactory、DA/LG适配、上下文、Skill加载
  execution/                      # [计划 R2] LocalProcessBackend；后续 SandboxBackend
  workspace/                      # [计划 R2] 文件修订、manifest、产物
  skills/                         # [计划 R2] ManualSkillResolver；后续Platform适配
  integrations/                   # [计划] 模型/API/MCP/数仓连接器
apps/platform/                    # [计划 P1] 只消费HTTP/SSE的最小页面/代理
skills/<skill-name>/SKILL.md       # 手工 Skill 源包，不是 Python 模块
scripts/                          # 生成/验证工具；不放业务运行逻辑
db/migrations/                    # 有序且可审阅的数据库迁移
docs/
  detailed-design.md              # 总详设
  agent-workflow.md               # 端到端流程/分支/DA-LG-RT职责
  coding-standards.md             # 本规范
  database.md                    # 字段与事务约束
  api/                           # OpenAPI、状态JSON、接入语义
  testing/                       # 分支测试注册表和生成的用例
  implementation-guide.md        # 实现任务包/验收与回交
  history/                       # 历史参考，不作为当前开发要求
tests/
  test_core.py                    # 现有纯核心测试
  test_contract_consistency.py    # 代码/接口/SQL一致性
  test_architecture_rules.py      # 架构/流程/审计/索引静态防漂移
  integration/                   # [计划] 真PG、进程、HTTP与DA/LG
  e2e/                           # [计划] 真实模型与Platform主流程
```

不建立模糊的巨型 utils/helpers/common 模块；公共代码以具体职责命名。Platform 不导入 `src/code_forge` 私有实现，不共享数据库 ORM model。

## 2. 依赖方向与接口

约束：core 五个模块只依赖标准库及彼此，禁止 import FastAPI、LangGraph、数据库驱动或 Platform。技术适配器依赖 core，core 不反向依赖适配器。入口/组合根负责注入具体依赖，核心构造函数不读取环境变量或自行打开连接。

传输层负责 schema/默认值、可信上下文、UUID/时间序列化和 HTTP 错误；应用服务负责顺序；领域代码负责状态规则；Repository 负责事务/CAS；ExecutionBackend 负责真实进程。不得在路由函数中塞图循环、SQL和进程管理。

Protocol 描述行为、超时/幂等语义与异常，而不只列方法名。公开 DTO 优先不可变 dataclass；动态 JSON 只用于明确的版本化 payload/扩展字段，不能到处传无结构 dict 取代契约。

LangGraph/DA 类型只能在 harness/适配器内流转，公开 API 与持久产品状态保持本项目类型。不要因框架事件名变化让 Platform 同步改写；用 EventAdapter 屏蔽。

## 3. Python 代码风格

- Python 3.12+，UTF-8、4空格，Ruff format；行长100，按工具自动格式化。
- 模块/函数/变量用 snake_case，类用 PascalCase，常量与枚举成员用 UPPER_SNAKE_CASE；公开事件值按 contracts 中小写点号格式。
- 禁止星号导入、多语句塞同一行、用导入时副作用启动任务或建数据库连接；导入顺序由 Ruff 管理。
- 公开接口、核心函数和重要适配边界写类型注解；docstring 说明输入/输出、事务归属、可重入/副作用和异常，不复述函数名。
- 避免泛化大类/隐式全局状态；函数专注一个业务动作。复杂度增加优先拆出明确服务/策略，不能只为减少行数拆出无法理解的间接层。
- 不在核心固定模型名称、API Key、机器绝对路径、用户 ID、连接 URL；配置由边界注入，真实密钥留在运行环境。
- 生产实现依赖必须锁版本并记录兼容证据；核心当前仅标准库，不能因为框架迁移给 core 增加依赖。

## 4. 状态、并发与异步

约束：Run 状态变化调用核心 state_machine；持久更新使用 `state_version` CAS，并在活跃执行时验证 Session.active_run_id、Run.active_attempt_id 与 epoch/lease。纯 Python 判断不是分布式锁。

同一 Session 的写入序列化，等待时保留归属。采用固定锁顺序 Session → Run → Attempt → Tool/Pending，短事务内提交；不得持数据库行锁调用 LLM、执行命令或下载文件。

每个后台任务必须有生命周期归属、异常回收、取消与关停策略，不能 fire-and-forget 静默丢异常。连接/进程/文件用明确关闭路径；同步或 CPU 重工作不能阻塞 API/心跳事件循环。

区分队列截止、总Run截止、模型调用超时、工具超时；默认超时可配置。CancelledError 和 LangGraph interrupt 不得被宽泛异常捕获吞掉；清理后重新传播或转换为已定义状态。

## 5. 工具、副作用、文件与安全边界

先持久意图再派发；同逻辑槽同参数复用结果，不同参数冲突。禁止把未知操作自动重试，禁止在 `finally` 中无条件报告成功。每种重试策略明确次数/退避/是否可安全重发。

使用参数化 SQL，不拼接用户输入；进程优先 argv，显式 shell 工具才允许 shell 字符串。Python 使用子进程，禁止 Worker 内 eval/exec。密钥不进入提示词、事件、审计身份或普通日志。

文件工具校验Workspace相对路径、符号链接和期望内容摘要；正式修订先提交内容再CAS指针。本机执行无法限制恶意脚本访问宿主，因此不得把路径包装当沙箱。日志与工具输出有界，不读取无限 stdout 到内存。

测试/构建结论带真实环境、命令、输入修订和日志。用户文件的已有修改不被无条件清理/reset覆盖。演示模型/fixture 只能用于明确标注的测试级别。

## 6. 数据库与审计规范

所有自管表均有 `date_created`、`created_by`、`date_updated`、`updated_by`；时间类型 timestamptz。新增时两组时间和主体相同；修改保留新增字段、写当前修改主体和DB时间；幂等无修改不更新审计。后台操作用 `system:runtime/<role>`，用户动作使用可信 actor_ref；没有可信主体时显式服务身份，不保存空串或冒充用户。

每张表和每个字段必须有 COMMENT ON；索引仅主键、普通 B-tree、唯一索引，支持普通复合列，不使用条件/表达式/INCLUDE覆盖/GIN/GiST等特殊形式。外键、CHECK 和 enum 是完整性约束，不是新增索引类别；不得因索引规范擅自删除它们。

索引服务实际查询，避免重复左前缀索引堆积。移除条件索引之后，唯一活动任务/尝试由活动指针、行锁和CAS落实，不能删除索引却忘记替代规则。详见 [database.md](database.md)。

迁移有序命名 `NNN_description.sql`，已在共享环境执行的迁移不可改写，追加迁移并评估历史数据回填；当前 001 尚未部署，可以直接修订。ORM 必须映射新审计字段，不私自恢复旧命名。

## 7. 错误、事件与观测

外部错误用统一 ErrorCode/HTTP映射，不能直接把异常栈当API结果。日志保留 request_id/run_id/attempt/operation 等关联ID，不记录秘密或隐藏推理。业务失败、系统故障、取消和未知结果分别处理。

事件遵循OpenAPI的封闭payload；新增事件同步契约/生成物/案例，不能只往前端发临时dict。审计时间是记录写入时间，occurred_at是事件业务发生时间，含义不同。新增/修改字段不在请求体中由不可信客户端自由赋值。

## 8. 测试、评审与交接

每项分支实现关联 TC-Bxx，用例说明前置、动作、状态/事件/数据库/文件断言。核心单元、真适配器、真框架、真模型、集群故障证据分别报告；不能把结构覆盖检查当成48个流程已通过。

提交前执行核心测试、格式/静态检查、契约重新生成后的差异检查；SQL改动增加字段备注/审计/索引类型验证，并在实现阶段跑真实PG迁移/并发测试。时间相关测试使用可控时钟与DB事务时间，不靠长sleep碰运气。

架构变更应同时提交：原因/失败证据、受影响流程与TC编号、接口/SQL变化、迁移/兼容处理、更新后的文档与测试。PR/回交说明只声称实际实现和验证的能力，明确剩余工作。
