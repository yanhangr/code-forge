# Agent 流程分支测试用例

来源：[JSON 注册表](agent-workflow-cases.json)。流程：[完整工作流程](../agent-workflow.md)。

状态 SPEC_ONLY 表示完整用例尚待实现/执行。core_evidence 只证明其中部分纯核心规则，不能作为集成通过记录。L0 为核心单元，L1 为数据库/执行/接口集成，L2 为真实框架链路，L3 为真实模型任务。

| 用例 | 流程分支 | 名称 | 验证级别 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-B01 | F01 / B01 | 校验拒绝 | L1 | SPEC_ONLY |
| TC-B02 | F01 / B02 | 授权拒绝 | L0+L1 | SPEC_ONLY |
| TC-B03 | F01 / B03 | 会话或作用域不匹配 | L1 | SPEC_ONLY |
| TC-B04 | F01 / B04 | 幂等复用旧任务 | L0+L1 | SPEC_ONLY |
| TC-B05 | F01 / B05 | 幂等内容冲突 | L0+L1 | SPEC_ONLY |
| TC-B06 | F01 / B06 | 快照解析失败 | L1 | SPEC_ONLY |
| TC-B07 | F01 / B07 | 持久接受与并发重复 | L1 | SPEC_ONLY |
| TC-B08 | F01 / B08 | 队首领取执行权 | L1 | SPEC_ONLY |
| TC-B09 | F01 / B09 | 等待或资源占用时排队 | L1 | SPEC_ONLY |
| TC-B10 | F01 / B10 | 排队期限和执行前失效 | L1 | SPEC_ONLY |
| TC-B11 | F02 / B11 | 新 Run 组装 Agent 图 | L2 | SPEC_ONLY |
| TC-B12 | F02 / B12 | 原 Run checkpoint 恢复 | L2 | SPEC_ONLY |
| TC-B13 | F02 / B13 | 运行图或模型配置不兼容 | L2 | SPEC_ONLY |
| TC-B14 | F02 / B14 | 纯对话直接完成 | L2+L3 | SPEC_ONLY |
| TC-B15 | F02 / B15 | 计划更新与实际执行分离 | L2+L3 | SPEC_ONLY |
| TC-B16 | F02 / B16 | Skill 激活与非法候选 | L2 | SPEC_ONLY |
| TC-B17 | F02 / B17 | 信息不足时挂起 | L2+L3 | SPEC_ONLY |
| TC-B18 | F02 / B18 | 模型请求工具先持久化 | L2 | SPEC_ONLY |
| TC-B19 | F02 / B19 | 只读子任务与失败合并 | L2 | SPEC_ONLY |
| TC-B20 | F02 / B20 | 上下文压缩成功与失败 | L2 | SPEC_ONLY |
| TC-B21 | F02 / B21 | 模型限流与重试耗尽 | L2 | SPEC_ONLY |
| TC-B22 | F02 / B22 | 步数预算到达 | L2 | SPEC_ONLY |
| TC-B23 | F03 / B23 | 工具输入或能力拒绝 | L1+L2 | SPEC_ONLY |
| TC-B24 | F03 / B24 | 已存在操作查回 | L1+L2 | SPEC_ONLY |
| TC-B25 | F03 / B25 | 同调用槽参数冲突 | L1 | SPEC_ONLY |
| TC-B26 | F03 / B26 | Python 正常执行并落盘 | L1+L3 | SPEC_ONLY |
| TC-B27 | F03 / B27 | 确定失败后修改再测 | L1+L3 | SPEC_ONLY |
| TC-B28 | F03 / B28 | 长外部工具挂起恢复 | L1+L2 | SPEC_ONLY |
| TC-B29 | F03 / B29 | 远端成功回包丢失 | L1+L2 | SPEC_ONLY |
| TC-B30 | F03 / B30 | 输出溢出与存储失败 | L1 | SPEC_ONLY |
| TC-B31 | F04 / B31 | 证据充分的完成 | L1+L3 | SPEC_ONLY |
| TC-B32 | F04 / B32 | 仍需验证时继续 | L2+L3 | SPEC_ONLY |
| TC-B33 | F04 / B33 | 部分完成或业务阻塞 | L2+L3 | SPEC_ONLY |
| TC-B34 | F04 / B34 | 终态提交中断 | L1+L2 | SPEC_ONLY |
| TC-B35 | F05 / B35 | 所有可取消状态及重试 | L0+L1 | SPEC_ONLY |
| TC-B36 | F05 / B36 | 超时与未知进程优先核验 | L1 | SPEC_ONLY |
| TC-B37 | F05 / B37 | 有效新回应恢复 | L0+L1+L2 | SPEC_ONLY |
| TC-B38 | F06 / B38 | 安全边界故障接管 | L1+L2 | SPEC_ONLY |
| TC-B39 | F06 / B39 | 未知副作用不重放 | L1+L2 | SPEC_ONLY |
| TC-B40 | F06 / B40 | 旧执行者提交拒绝 | L0+L1 | SPEC_ONLY |
| TC-B41 | F07 / B41 | 流式事件与断线补发 | L1 | SPEC_ONLY |
| TC-B42 | F07 / B42 | 错误事件游标 | L1 | SPEC_ONLY |
| TC-B43 | F07 / B43 | 慢客户端和终态关闭 | L1 | SPEC_ONLY |
| TC-B44 | F07 / B44 | 多轮会话继承文件 | L1+L3 | SPEC_ONLY |
| TC-B45 | F07 / B45 | 新轮 Skill 升级与旧指令清理 | L2 | SPEC_ONLY |
| TC-B46 | F07 / B46 | 失败图的下一轮 | L2 | SPEC_ONLY |
| TC-B47 | F05 / B47 | 回应的幂等复用 | L1 | SPEC_ONLY |
| TC-B48 | F05 / B48 | 回应冲突或越界 | L1 | SPEC_ONLY |

## TC-B01：校验拒绝

对应分支：F01 / B01；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 已有默认作用域；准备空输入、超长输入及缺少幂等键三组请求

**操作/故障注入**

- 逐组提交 createRun

**断言**

- 400/422 INVALID_REQUEST
- 无新 Run/快照/事件
- 不调用模型

计划自动化位置：`tests/integration/test_flow_01.py::test_b01`（尚未建立）。

## TC-B02：授权拒绝

对应分支：F01 / B02；验证级别：L0+L1；状态：SPEC_ONLY。

**前置条件**

- AuthorizationPort 替换为拒绝实现；Session 存在

**操作/故障注入**

- 提交有效任务

**断言**

- CAPABILITY_DENIED
- resolver/repository.accept_once 均未调用
- 状态未改变

计划自动化位置：`tests/integration/test_flow_01.py::test_b02`（尚未建立）。

已存在的部分核心证据：
- [AcceptanceTests.test_authorization_precedes_resolution](../../tests/test_core.py)

## TC-B03：会话或作用域不匹配

对应分支：F01 / B03；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- Session 仅属于 scope-a；请求使用不存在 ID 或 scope-b

**操作/故障注入**

- 提交或读取任务

**断言**

- SESSION_NOT_FOUND
- 不返回其他 scope 数据
- 不接受 Run

计划自动化位置：`tests/integration/test_flow_01.py::test_b03`（尚未建立）。

## TC-B04：幂等复用旧任务

对应分支：F01 / B04；验证级别：L0+L1；状态：SPEC_ONLY。

**前置条件**

- 同键请求已接受且 Skill 源目录随后更新

**操作/故障注入**

- 用相同规范化请求和幂等键重试

**断言**

- 返回原 Run 且 reused=true
- 不再次解析 Skill
- 审计字段/事件/队列序号不增加

计划自动化位置：`tests/integration/test_flow_01.py::test_b04`（尚未建立）。

已存在的部分核心证据：
- [AcceptanceTests.test_retry_does_not_resolve_new_skill](../../tests/test_core.py)

## TC-B05：幂等内容冲突

对应分支：F01 / B05；验证级别：L0+L1；状态：SPEC_ONLY。

**前置条件**

- 已接受一条含代码空白/Skill 顺序的请求

**操作/故障注入**

- 复用原键但改 input 或 Skill 顺序

**断言**

- 409 IDEMPOTENCY_CONFLICT
- 原输入/快照保持不变

计划自动化位置：`tests/integration/test_flow_01.py::test_b05`（尚未建立）。

已存在的部分核心证据：
- [AcceptanceTests.test_key_conflict_and_input_whitespace_preserved](../../tests/test_core.py)

## TC-B06：快照解析失败

对应分支：F01 / B06；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 分别准备缺失包、非法路径、版本不兼容、存储不可用四组输入

**操作/故障注入**

- 提交指定 Skill 的任务

**断言**

- SKILL_NOT_FOUND/SKILL_INCOMPATIBLE/DEPENDENCY_UNAVAILABLE
- 无 202
- 不引用半写入包

计划自动化位置：`tests/integration/test_flow_01.py::test_b06`（尚未建立）。

## TC-B07：持久接受与并发重复

对应分支：F01 / B07；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 真实 PG；八个相同请求同时提交

**操作/故障注入**

- 并发接受后关闭 API 进程并重启查询

**断言**

- 仅一个 Run/接受事件和一个 run_seq
- 四个审计字段齐全
- 202 后数据可读

计划自动化位置：`tests/integration/test_flow_01.py::test_b07`（尚未建立）。

已存在的部分核心证据：
- [AcceptanceTests.test_concurrent_duplicate_acceptance](../../tests/test_core.py)

## TC-B08：队首领取执行权

对应分支：F01 / B08；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- Session 无主任务，存在两条顺序 Run；两个调度器同时领取

**操作/故障注入**

- 执行领取事务

**断言**

- 只有队首成为 RUNNING
- active_run_id/active_attempt_id/epoch 一致
- 一个合法执行者

计划自动化位置：`tests/integration/test_flow_01.py::test_b08`（尚未建立）。

## TC-B09：等待或资源占用时排队

对应分支：F01 / B09；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 参数化主 Run 为 RUNNING/WAITING_USER/WAITING_EXTERNAL，另设资源不足组

**操作/故障注入**

- 尝试领取后序 Run

**断言**

- 后序仍 QUEUED
- 不调用模型、不重复建立 Attempt
- 资源恢复才重新判断

计划自动化位置：`tests/integration/test_flow_01.py::test_b09`（尚未建立）。

## TC-B10：排队期限和执行前失效

对应分支：F01 / B10；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 两组队首：期限已过；固定配置永久不可用

**操作/故障注入**

- 扫描并尝试执行

**断言**

- 分别 TIMED_OUT/FAILED
- 输出明确原因和终态事件
- 清理队列归属
- 无工具副作用

计划自动化位置：`tests/integration/test_flow_01.py::test_b10`（尚未建立）。

## TC-B11：新 Run 组装 Agent 图

对应分支：F02 / B11；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 可运行 Run，有 AgentSpec、工具与手工 Skill 快照

**操作/故障注入**

- 通过真实 AgentFactory/LG 启动一轮

**断言**

- 按锁定配置组装一次
- LG 执行模型节点
- 只读取当前候选摘要
- 不另建竞争模型循环

计划自动化位置：`tests/integration/test_flow_02.py::test_b11`（尚未建立）。

## TC-B12：原 Run checkpoint 恢复

对应分支：F02 / B12；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 图已持久一个工具结果，Run 因可恢复原因暂停

**操作/故障注入**

- 恢复原 Run 与 checkpoint

**断言**

- input 不追加两次
- 沿原快照/调用槽继续
- 已完成工具不重跑

计划自动化位置：`tests/integration/test_flow_02.py::test_b12`（尚未建立）。

## TC-B13：运行图或模型配置不兼容

对应分支：F02 / B13；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 固定 runtime_ref 无兼容实例或模型 profile 无效

**操作/故障注入**

- 尝试组装/恢复

**断言**

- 不以新图强行读取旧状态
- 有界等待或 FAILED
- 不静默用假模型/新 Skill

计划自动化位置：`tests/integration/test_flow_02.py::test_b13`（尚未建立）。

## TC-B14：纯对话直接完成

对应分支：F02 / B14；验证级别：L2+L3；状态：SPEC_ONLY。

**前置条件**

- 任务无需工具；模型能够直接回答

**操作/故障注入**

- 提交普通问题并观察完整图事件

**断言**

- 无计划/Python/Skill 的强制调用
- message.completed 与 run.finished 正常提交

计划自动化位置：`tests/integration/test_flow_02.py::test_b14`（尚未建立）。

## TC-B15：计划更新与实际执行分离

对应分支：F02 / B15；验证级别：L2+L3；状态：SPEC_ONLY。

**前置条件**

- 多步骤 Coding 任务；计划中含实现和测试

**操作/故障注入**

- 模型更新 todo 后继续执行

**断言**

- 计划变化不等于任务终态
- 没有测试证据不能声称已测
- 进入工具反馈循环

计划自动化位置：`tests/integration/test_flow_02.py::test_b15`（尚未建立）。

## TC-B16：Skill 激活与非法候选

对应分支：F02 / B16；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 一合法锁定包，另有未授权候选及 digest 被改的快照

**操作/故障注入**

- 分别请求加载合法包/快照外包/损坏包

**断言**

- 合法包按需加载并发 skill.activated
- 其他明确拒绝
- 不回读新源包绕过校验

计划自动化位置：`tests/integration/test_flow_02.py::test_b16`（尚未建立）。

## TC-B17：信息不足时挂起

对应分支：F02 / B17；验证级别：L2+L3；状态：SPEC_ONLY。

**前置条件**

- 任务缺少关键参数；默认授权不要求审批

**操作/故障注入**

- 模型请求澄清必要信息

**断言**

- 保存 clarification pending
- WAITING_USER 与 run.waiting
- LG interrupt 前无重复副作用

计划自动化位置：`tests/integration/test_flow_02.py::test_b17`（尚未建立）。

## TC-B18：模型请求工具先持久化

对应分支：F02 / B18；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 模型返回一次写文件或 Python 调用

**操作/故障注入**

- 在模型结果落盘前后设置故障点

**断言**

- 调用列表/逻辑槽持久之前不派发
- PREPARED 在执行前存在
- 无第二套 LLM loop

计划自动化位置：`tests/integration/test_flow_02.py::test_b18`（尚未建立）。

## TC-B19：只读子任务与失败合并

对应分支：F02 / B19；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 主任务配置 Explore/Review、禁止递归并设置父预算

**操作/故障注入**

- 正常完成一组，另组子任务超时/父取消

**断言**

- 子图独立上下文、不能写或递归
- 结果带来源
- 失败/取消可见且费用归父任务

计划自动化位置：`tests/integration/test_flow_02.py::test_b19`（尚未建立）。

## TC-B20：上下文压缩成功与失败

对应分支：F02 / B20；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 长工具日志逼近上下文预算；另组压缩调用失败

**操作/故障注入**

- 继续 Agent 模型循环

**断言**

- 成功保留目标/来源/待决/Skill
- 失败保留旧有效上下文并有界处理
- 不清空后继续写

计划自动化位置：`tests/integration/test_flow_02.py::test_b20`（尚未建立）。

## TC-B21：模型限流与重试耗尽

对应分支：F02 / B21；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 注入 429/临时超时后恢复及持续失败两组

**操作/故障注入**

- 让 LG 模型节点请求模型

**断言**

- 仅重试当前模型请求且退避有上限
- 不重复工具
- 耗尽 FAILED 且有明确错误

计划自动化位置：`tests/integration/test_flow_02.py::test_b21`（尚未建立）。

## TC-B22：步数预算到达

对应分支：F02 / B22；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 任务持续重复调用工具，已接近步数上限

**操作/故障注入**

- 继续循环至预算门

**断言**

- 不派发超预算操作
- partial/blocked 说明未完成项
- 不无限压缩或 spawn 续命

计划自动化位置：`tests/integration/test_flow_02.py::test_b22`（尚未建立）。

## TC-B23：工具输入或能力拒绝

对应分支：F03 / B23；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 工具名不存在、参数错或能力被拒绝三组

**操作/故障注入**

- 通过 ToolRouter 请求操作

**断言**

- 结构化错误供 DA 重新规划
- 不创建成功结果、不执行进程
- 无宿主备用通道

计划自动化位置：`tests/integration/test_flow_03.py::test_b23`（尚未建立）。

## TC-B24：已存在操作查回

对应分支：F03 / B24；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 同逻辑槽对应 SUCCEEDED、RUNNING、UNKNOWN 三组账本

**操作/故障注入**

- 恢复工具调用

**断言**

- 成功复用结果
- 运行中附着原句柄
- UNKNOWN 进入核验
- 不再次派发

计划自动化位置：`tests/integration/test_flow_03.py::test_b24`（尚未建立）。

## TC-B25：同调用槽参数冲突

对应分支：F03 / B25；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 已有 PREPARED 操作及 params_digest

**操作/故障注入**

- 使用相同逻辑槽但不同命令参数提交

**断言**

- STATE_CONFLICT
- 原操作/摘要不变
- 不以覆盖输入方式执行新效果

计划自动化位置：`tests/integration/test_flow_03.py::test_b25`（尚未建立）。

## TC-B26：Python 正常执行并落盘

对应分支：F03 / B26；验证级别：L1+L3；状态：SPEC_ONLY。

**前置条件**

- 本机 LocalProcessBackend 可用；任务计算平方和并写结果文件

**操作/故障注入**

- 真实运行 Python，读取 stdout/退出码/文件并返回 Agent

**断言**

- 结果为338350
- 退出码0
- 文件/日志可靠保存后工具成功
- LG/DA根据真实反馈继续

计划自动化位置：`tests/integration/test_flow_03.py::test_b26`（尚未建立）。

## TC-B27：确定失败后修改再测

对应分支：F03 / B27；验证级别：L1+L3；状态：SPEC_ONLY。

**前置条件**

- 脚本或代码测试有可复现错误

**操作/故障注入**

- 运行得到非零退出后由 Agent 修复并再次执行

**断言**

- 原工具 FAILED 而 Run 可继续
- 新参数使用新逻辑槽
- 修复后的测试证据对应当前修订

计划自动化位置：`tests/integration/test_flow_03.py::test_b27`（尚未建立）。

## TC-B28：长外部工具挂起恢复

对应分支：F03 / B28；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 外部测试服务返回稳定查询句柄；准备成功/失败回调

**操作/故障注入**

- 挂起后发送重复回调并恢复

**断言**

- WAITING_EXTERNAL 释放执行槽但保留主 Run
- 回调幂等
- 失败作为工具结果
- 无并发图执行者

计划自动化位置：`tests/integration/test_flow_03.py::test_b28`（尚未建立）。

## TC-B29：远端成功回包丢失

对应分支：F03 / B29；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 远端已产生效果，本地未记录结果时断线

**操作/故障注入**

- 恢复/超时处理该操作

**断言**

- 工具 UNKNOWN
- 用原键/句柄核验
- 不能因异常重跑
- 无核验能力时 WAITING_USER

计划自动化位置：`tests/integration/test_flow_03.py::test_b29`（尚未建立）。

## TC-B30：输出溢出与存储失败

对应分支：F03 / B30；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 大 stdout/stderr、文件基线冲突、结果存储失败三组

**操作/故障注入**

- 执行并尝试提交结果

**断言**

- 输出有截断且管道不卡
- 冲突不覆盖
- 存储失败不确认成功或提前发完成事件

计划自动化位置：`tests/integration/test_flow_03.py::test_b30`（尚未建立）。

## TC-B31：证据充分的完成

对应分支：F04 / B31；验证级别：L1+L3；状态：SPEC_ONLY。

**前置条件**

- 工作完成且测试/计算证据与最终修订相符

**操作/故障注入**

- 最终提交结果

**断言**

- SUCCEEDED+completed
- 输出/引用/终态事件可靠
- 归属指针清空，下一 Run 可领取

计划自动化位置：`tests/integration/test_flow_04.py::test_b31`（尚未建立）。

## TC-B32：仍需验证时继续

对应分支：F04 / B32；验证级别：L2+L3；状态：SPEC_ONLY。

**前置条件**

- 模型提出完成但未测，或测试后代码再次改变，或子任务未完成

**操作/故障注入**

- 运行 before-finalize 证据检查

**断言**

- 继续同一图完成验证
- 不提前 run.finished
- 不可把过期测试当当前修订证据

计划自动化位置：`tests/integration/test_flow_04.py::test_b32`（尚未建立）。

## TC-B33：部分完成或业务阻塞

对应分支：F04 / B33；验证级别：L2+L3；状态：SPEC_ONLY。

**前置条件**

- 部分工作完成或输入数据/环境缺失，已无允许的继续路径

**操作/故障注入**

- 收尾输出剩余项和原因

**断言**

- SUCCEEDED+partial/blocked
- 不冒充测试通过
- 真实已生成文件仍保留

计划自动化位置：`tests/integration/test_flow_04.py::test_b33`（尚未建立）。

## TC-B34：终态提交中断

对应分支：F04 / B34；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 模型最终候选已完成，准备输出/文件/PG 提交故障

**操作/故障注入**

- 故障后运行对账或提交重试

**断言**

- 只重试提交/核验，不重新执行模型写工具
- 最终事件仅在可靠提交后可见

计划自动化位置：`tests/integration/test_flow_04.py::test_b34`（尚未建立）。

## TC-B35：所有可取消状态及重试

对应分支：F05 / B35；验证级别：L0+L1；状态：SPEC_ONLY。

**前置条件**

- 分别 QUEUED/RUNNING/WAITING_USER/WAITING_EXTERNAL/RECOVERING/CANCELLING/终态

**操作/故障注入**

- 请求取消，执行中组包含子孙进程

**断言**

- 有效新请求转 CANCELLING 后核验停止再 CANCELLED
- 其他幂等空操作
- 审计/事件不重复

计划自动化位置：`tests/integration/test_flow_05.py::test_b35`（尚未建立）。

已存在的部分核心证据：
- [StateTests.test_cancel_idempotent_and_not_success](../../tests/test_core.py)
- [PlatformApiTransportTests.test_cancel_message_is_public_and_idempotent](../../tests/test_transport_user_binding.py)

## TC-B36：超时与未知进程优先核验

对应分支：F05 / B36；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 总期限到达，参数化进程可停止/结果未知/已 CANCELLING

**操作/故障注入**

- 启动超时清理与状态推进

**断言**

- 已核验退出可 TIMED_OUT
- 未知先核验并保留 Workspace
- 已有取消按 CANCELLED 收尾

计划自动化位置：`tests/integration/test_flow_05.py::test_b36`（尚未建立）。

## TC-B37：有效新回应恢复

对应分支：F05 / B37；验证级别：L0+L1+L2；状态：SPEC_ONLY。

**前置条件**

- WAITING_USER/WAITING_EXTERNAL，pending 未解决，版本匹配

**操作/故障注入**

- 原子消费回应并重新调度

**断言**

- 原 Run QUEUED 后 RUNNING
- input 不重复
- LG resume 使用匹配结果
- 新审计修改字段更新

计划自动化位置：`tests/integration/test_flow_05.py::test_b37`（尚未建立）。

已存在的部分核心证据：
- [StateTests.test_wait_resume_and_completion](../../tests/test_core.py)

## TC-B38：安全边界故障接管

对应分支：F06 / B38；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 参数化：模型响应未持久且未派发；工具结果已持久但 checkpoint 未提交

**操作/故障注入**

- 终止 Worker，另一个执行者接管

**断言**

- 前者可重算模型，后者复用结果
- 关闭旧 Attempt 增 epoch
- 图沿原快照继续

计划自动化位置：`tests/integration/test_flow_06.py::test_b38`（尚未建立）。

## TC-B39：未知副作用不重放

对应分支：F06 / B39；验证级别：L1+L2；状态：SPEC_ONLY。

**前置条件**

- 本机可能残留进程或外部状态不可查

**操作/故障注入**

- Worker 接管核验失败

**断言**

- Run RECOVERING→WAITING_USER
- 工具 UNKNOWN
- 不释放未知 writer 的工作区给下一 Run

计划自动化位置：`tests/integration/test_flow_06.py::test_b39`（尚未建立）。

已存在的部分核心证据：
- [StateTests.test_unknown_execution_waits_for_reconciliation](../../tests/test_core.py)

## TC-B40：旧执行者提交拒绝

对应分支：F06 / B40；验证级别：L0+L1；状态：SPEC_ONLY。

**前置条件**

- 旧 Worker 持有旧 epoch，已发生接管

**操作/故障注入**

- 旧 Worker 尝试改 Run/checkpoint/pending writes/发布修订

**断言**

- active指针/epoch/CAS在同一事务拒绝
- 新状态不被覆盖
- 旧本机目录不能成为正式修订

计划自动化位置：`tests/integration/test_flow_06.py::test_b40`（尚未建立）。

已存在的部分核心证据：
- [StateTests.test_stale_writer_rejected](../../tests/test_core.py)

## TC-B41：流式事件与断线补发

对应分支：F07 / B41；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 运行中有消息/工具事件，游标已记录

**操作/故障注入**

- 断开后连接另一个 API 连接并补发

**断言**

- seq 去重与顺序正确
- message.completed替换临时文本
- 快照/水位一致
- Run不中止

计划自动化位置：`tests/integration/test_flow_07.py::test_b41`（尚未建立）。

## TC-B42：错误事件游标

对应分支：F07 / B42；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 准备格式错、其他Run游标、超过水位三组

**操作/故障注入**

- 请求事件流

**断言**

- INVALID_EVENT_CURSOR
- 不默默跳历史，不将错误写进已打开流的普通JSON响应

计划自动化位置：`tests/integration/test_flow_07.py::test_b42`（尚未建立）。

## TC-B43：慢客户端和终态关闭

对应分支：F07 / B43；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 工具持续输出，客户端读取慢；另组Run已结束

**操作/故障注入**

- 限制读速/断开/收到终态后停止订阅

**断言**

- 有界背压不影响Worker
- 断线可恢复
- 终态不无限自动重连

计划自动化位置：`tests/integration/test_flow_07.py::test_b43`（尚未建立）。

## TC-B44：多轮会话继承文件

对应分支：F07 / B44；验证级别：L1+L3；状态：SPEC_ONLY。

**前置条件**

- 前轮已提交文件与确认历史，后轮先排队

**操作/故障注入**

- 前轮结束后执行后轮追问

**断言**

- 新的Run/快照
- 执行时取前轮最新修订
- 历史不丢且无并发writer

计划自动化位置：`tests/integration/test_flow_07.py::test_b44`（尚未建立）。

## TC-B45：新轮 Skill 升级与旧指令清理

对应分支：F07 / B45；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 旧Run用了v1，手工源改v2，摘要可能含v1指令

**操作/故障注入**

- 重试旧请求并提交新一轮

**断言**

- 旧请求仍v1
- 新Run锁v2
- 旧正文/摘要不作为当前活动指令
- 不破坏消息工具配对

计划自动化位置：`tests/integration/test_flow_07.py::test_b45`（尚未建立）。

## TC-B46：失败图的下一轮

对应分支：F07 / B46；验证级别：L2；状态：SPEC_ONLY。

**前置条件**

- 前Run取消/失败且checkpoint含pending工具请求

**操作/故障注入**

- 在同Session提交新Run

**断言**

- 不执行旧待决工具
- 安全收尾或新Thread承接确认事实
- Session与文件继续可用

计划自动化位置：`tests/integration/test_flow_07.py::test_b46`（尚未建立）。

## TC-B47：回应的幂等复用

对应分支：F05 / B47；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 回应已消费并推进状态，原expected_version已旧

**操作/故障注入**

- 同response_key同内容再次提交

**断言**

- 返回当前Run，不再次恢复、不改审计字段、不重复回应事件

计划自动化位置：`tests/integration/test_flow_05.py::test_b47`（尚未建立）。

## TC-B48：回应冲突或越界

对应分支：F05 / B48；验证级别：L1；状态：SPEC_ONLY。

**前置条件**

- 同键不同内容、错误pending_id、新回应版本冲突三组

**操作/故障注入**

- 提交回应

**断言**

- 409/404 明确错误
- 原待决项/运行状态不变，拒绝跨scope操作

计划自动化位置：`tests/integration/test_flow_05.py::test_b48`（尚未建立）。
