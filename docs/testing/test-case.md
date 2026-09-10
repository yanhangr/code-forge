可以。我建议测试不要只测“单个 Skill 能不能跑通”，而是围绕我们这套架构里最关键的能力来设计：**Skill 编排、Session/Run 持久化、多用户隔离、SubAgent、动态 Skill、Tool 路由、故障恢复**。

尤其第一批测试，最好故意设计成“会暴露架构问题”的 Case，而不是简单 Happy Path。

## 一、先做一个核心综合案例：一个 Skill 编排多个 Skill

可以先设计一个总 Skill：

```text
risk-analysis-orchestrator

目标：
根据用户输入的需求完成一次完整的数据分析。

内部需要依次使用：

1. requirement-understanding
2. context-collector
3. sql-analysis
4. result-validator
5. report-generator
```

逻辑：

```text
用户
 │
 │ “分析近三个月某指标下降的原因”
 ▼
risk-analysis-orchestrator
 │
 ├─ Skill 1：requirement-understanding
 │      ↓
 │   输出 AnalysisRequest
 │
 ├─ Skill 2：context-collector
 │      ↓
 │   获取字段、表、业务口径
 │
 ├─ Skill 3：sql-analysis
 │      ↓
 │   调用 SQL Tool
 │
 ├─ Skill 4：result-validator
 │      ↓
 │   检查结果是否可信
 │
 └─ Skill 5：report-generator
        ↓
     最终结果
```

这个 Case 一次就能测很多东西。

### Case 01：Skill 顺序编排

验证：

```text
A → B → C → D → E
```

是否严格按照定义执行。

测试输入：

> 分析近三个月客户动支率下降原因。

预期：

```text
requirement-understanding
必须先执行

context-collector
第二执行

sql-analysis
不能提前执行

result-validator
SQL有结果后执行

report-generator
最后执行
```

重点不是最终答案对不对，而是检查 Event：

```text
SKILL_STARTED A
SKILL_COMPLETED A

SKILL_STARTED B
SKILL_COMPLETED B

...
```

这样可以验证 Skill 编排是否真正可控。

---

## 二、Case 02：Skill 之间参数是否正确传递

例如 Skill A 输出：

```json
{
  "metric": "drawdown_rate",
  "period": "2026-06~2026-08",
  "dimensions": [
    "risk_level",
    "customer_type"
  ]
}
```

Skill B 必须拿到这个结果。

然后 Skill B 输出：

```json
{
  "tables": ["loan_customer", "loan_drawdown"],
  "join_key": "customer_id"
}
```

Skill C 再使用。

重点测：

> Skill 之间是不是通过结构化 State 传值，而不是靠自然语言“猜上一段内容”。

建议定义：

```text
SkillResult
├── status
├── structured_output
├── artifacts
├── warnings
└── next_context
```

这是很重要的测试。

---

# 三、Case 03：子 Skill 失败后的行为

例如：

```text
A requirement
↓
B context collector
↓
查不到字段
↓
FAILED
```

此时总 Skill 不应该直接继续：

```text
sql-analysis
```

应该：

```text
B
↓
NEED_USER_INPUT
↓
LangGraph interrupt
↓
用户补充
↓
从 B 恢复
```

需要验证：

```text
A 不重新执行
B 从正确位置继续
C 尚未启动
```

这个 Case 可以直接检验 LangGraph checkpoint / interrupt 设计。

---

# 四、Case 04：Skill 内部有条件分支

例如：

```text
analysis-orchestrator
         │
         ▼
 requirement-understanding
         │
         ▼
是否需要数据？
    │           │
   YES          NO
    │           │
    ▼           ▼
sql-analysis   knowledge-analysis
    │           │
    └─────┬─────┘
          ▼
 report-generator
```

测试两个请求：

### 输入 A

> 最近三个月动支率为什么下降？

应该走：

```text
SQL
```

### 输入 B

> DID 和 OLS 有什么区别？

应该走：

```text
knowledge-analysis
```

而不是为了“有 SQL Tool”什么问题都查数据库。

---

# 五、Case 05：Skill 并行编排

这是很值得测的。

例如一个总 Skill：

```text
customer-analysis
```

可以同时执行：

```text
             ┌─ risk-analysis
             │
Input ───────┼─ behavior-analysis
             │
             └─ pricing-analysis
                      │
                      ▼
                  synthesis
```

重点验证：

```text
三个 Skill 是否可以并行

是否各自拥有独立上下文

执行完成以后是否正确 join

某一个失败时另外两个怎么办
```

可以设：

```text
risk-analysis     3秒
behavior-analysis 8秒
pricing-analysis  5秒
```

期望总体不是：

```text
3 + 8 + 5 = 16秒
```

而是接近：

```text
max(3,8,5)
```

然后再汇总。

---

# 六、Case 06：Skill 调 Skill，再调 SubAgent

这个更贴近你真实场景。

```text
Main Agent
    ↓
analysis-orchestrator Skill
    ↓
context-collector Skill
    ↓
Context Collector SubAgent
    ↓
自定义 LangGraph

R1 → R2 → R3 → R4 → R5 → R6
```

验证：

1. Skill 可以触发 SubAgent；
2. SubAgent 拿到明确输入；
3. SubAgent 不会重新调用自己；
4. SubAgent 最终只返回结构化结果；
5. 主 Agent 不继承 SubAgent 全部上下文。

这个 Case 能直接验证你以前 OpenCode 遇到的递归问题有没有真正解决。

---

# 七、Case 07：禁止 Skill 无限递归

专门造一个坏 Skill：

```text
skill-A
↓
调用 skill-B

skill-B
↓
调用 skill-A
```

平台必须阻止：

```text
A → B → A → B → ...
```

建议测试：

```text
max_skill_depth = 5
```

以及：

```text
execution_path:
A → B → A
```

检测到循环后：

```text
SKILL_RECURSION_DETECTED
```

而不是让 Token 一直烧。

这个非常值得第一版就测。

---

# 八、Case 08：Skill 版本热更新

例如：

```text
analysis-skill@v1
```

Run R1 开始：

```text
R1
↓
使用 v1
```

执行过程中管理员发布：

```text
analysis-skill@v2
```

预期：

```text
R1
仍然使用 v1

R2
开始使用 v2
```

不能出现：

```text
R1前半段用v1
后半段用v2
```

重点检查：

```text
run.skill_snapshot
```

---

# 九、Case 09：Skill 更新后同一个 Session 是否感知

例如：

```text
Session S1

Run1 → skill@v1
```

发布 v2。

然后同一个 Session：

```text
Run2
```

预期：

```text
重新刷新 Skill Catalog
↓
发现 v2
↓
加载 v2
```

这个就是验证你之前要求的：

> 动态 Skill 加载。

---

# 十、Case 10：Skill 权限隔离

例如：

```text
System Skill
Team A Skill
Team B Skill
Project Skill
User Skill
```

用户 A：

```text
Tenant A
Team A
```

不能看到：

```text
Team B Skill
```

测试：

```text
User A:
load_skill("team-b-secret")
```

预期：

```text
PERMISSION_DENIED
```

而不是仅仅“不出现在 prompt 里”。

因为模型有可能猜到 Skill ID。

---

# 十一、Session 层必须测的案例

## Case 11：同一 Session 请求打到不同 Pod

```text
Run1 → Worker A
Run2 → Worker C
Run3 → Worker B
```

预期：

```text
Conversation完整连续
Skill状态正确
Thread一致
```

这个 Case 必须作为自动化测试。

甚至可以故意：

```text
每次Run随机路由Worker
```

确认系统完全不依赖 sticky session。

---

# 十二、Case 12：Worker 执行中被 kill

非常关键。

```text
Run R1
 ↓
LLM
 ↓
Skill A
 ↓
Tool 1
 ↓
Checkpoint
 ↓
Tool 2
 ↓

kubectl delete pod worker-1
```

然后观察：

```text
新 Worker
↓
接管 Run
↓
同一个 thread_id
↓
恢复
```

需要检查：

```text
已完成 Tool 是否重复执行
Message 是否重复
Event 是否重复
```

这是生产前必须做的 Chaos Case。

---

# 十三、Case 13：同一个 Session 连续提交两个请求

例如用户快速输入：

```text
Message 1:
分析数据

Message 2:
再按风险等级拆分
```

此时：

```text
Run1 RUNNING
Run2 ?
```

建议预期：

```text
Run1 RUNNING
Run2 QUEUED
```

等：

```text
Run1 SUCCEEDED
```

再：

```text
Run2 RUNNING
```

验证：

> 同一个 LangGraph Thread 不能同时有两个写状态的主 Run。

---

# 十四、Case 14：两个用户同时打同一个 Worker

```text
Worker A
├── User A / Session A
└── User B / Session B
```

故意让两个 Agent 都：

```text
read memory
load skill
write cache
```

然后检查：

```text
A 看不到 B Message
A 看不到 B Skill
A 看不到 B Memory
A 看不到 B Artifact
```

这个是典型数据串线测试。

---

# 十五、Case 15：用户伪造 session_id

User A 调：

```http
POST /sessions/{User-B-Session}/messages
```

预期：

```text
403 / 404
```

绝对不能因为知道：

```text
session_id
```

就能访问。

同样测试：

```text
thread_id
run_id
artifact_id
workspace_id
```

全部必须有 owner/tenant 检查。

---

# 十六、Tool 层的测试也很重要

## Case 16：同一个 Tool 根据环境切换执行位置

例如：

```text
python tool
```

开发环境：

```text
execution_target = LOCAL
```

生产：

```text
execution_target = SANDBOX
```

Agent 的：

```text
Tool schema
prompt
Skill
Graph
```

都不改。

只改变：

```text
ToolExecutor
```

这就是验证我们 Execution Backend 抽象有没有设计正确。

---

# 十七、Case 17：SQL Tool 是 Remote，Python 是 Sandbox

未来测试：

```text
Agent
 │
 ├─ sql_query
 │     ↓
 │   Remote Service
 │
 └─ python
       ↓
     Sandbox
```

检查：

```text
Tool Router
```

是否按照 metadata 正确路由。

---

# 十八、Case 18：Tool 幂等

特别重要。

设计一个 Tool：

```text
send_notification()
```

第一次：

```text
tool_call_id=TC001
↓
执行成功
↓
Worker故意kill
```

恢复以后再次请求：

```text
TC001
```

预期：

```text
查询 tool_execution
↓
发现 SUCCEEDED
↓
直接返回历史result
```

实际通知只能发：

```text
1次
```

不能 2 次。

---

# 十九、Case 19：长 Tool + Cancel

模拟：

```text
python
↓
sleep(120)
```

用户执行：

```text
STOP
```

预期：

```text
Web
↓
Run Cancel
↓
Tool Cancel
↓
执行真正终止
↓
Run = CANCELLED
```

不能只：

```text
UI显示停止
```

后台 Python 还在跑。

---

# 二十、Context 测试

## Case 20：超长会话 Context Compaction

故意构造：

```text
Session 100轮
```

每轮放：

```text
大量tool结果
代码
SQL
Skill
```

观察：

```text
什么时候触发compaction
```

测试压缩以后：

```text
用户关键需求是否还记得
当前任务状态是否还在
Skill结果是否丢失
```

以及 Token 是否明显下降。

---

# 二十一、Case 21：SubAgent Context 不污染 Main Agent

让 SubAgent 搜索大量内容：

```text
100个文件
10万token
```

最终只输出：

```json
{
  "findings": [...],
  "evidence": [...]
}
```

检查 Main Agent context：

```text
不能包含SubAgent全部搜索过程
```

只能有：

```text
SubAgent Result
```

这个是 Deep Agent 很关键的价值。

---

# 二十二、Event 测试

## Case 22：完整 Run Event 顺序

一次普通执行至少应该看到：

```text
RUN_STARTED

MESSAGE_CREATED

MODEL_STARTED
MODEL_COMPLETED

SKILL_LOADED

TOOL_STARTED
TOOL_COMPLETED

MESSAGE_DELTA
MESSAGE_COMPLETED

RUN_COMPLETED
```

测试：

```text
event_id
run_id
session_id
timestamp
sequence
```

是否正确。

---

# 二十三、Case 23：Web刷新重新接 Stream

执行：

```text
Run R100
```

Agent 正在输出。

浏览器：

```text
刷新
```

重新：

```text
GET /runs/R100/events?after=event-15
```

应该继续：

```text
event-16
event-17
...
```

不能：

```text
前面全部丢失
```

也不能：

```text
全部重复一遍
```

---

# 二十四、再设计几个“复杂业务型案例”

这些比单元测试更接近 Agent 实战。

### Case 24：需求不完整

用户：

> 帮我分析这个指标。

Agent 应该：

```text
Requirement Skill
↓
发现缺少指标名/时间
↓
interrupt
↓
询问用户
```

而不是乱猜。

---

### Case 25：Context Collector 无召回

```text
Code search
= 0

Wiki search
= 0
```

应该：

```text
CONTEXT_INSUFFICIENT
↓
询问用户/带假设继续
```

而不是凭模型知识编代码。

这直接可以复用你过去 Context Collector 的 Bad Case。

---

### Case 26：两路 Context 冲突

```text
代码：
字段叫 customer_level

Wiki：
字段叫 risk_level
```

应该：

```text
CONTEXT_CONFLICT
```

然后根据规则：

```text
代码优先
```

或者：

```text
用户确认
```

测试整个 conflict resolution。

---

# 二十五、建议做一个专门的“Orchestrator Skill”压测案例

我很推荐把这个作为第一阶段 Demo。

设计：

```text
comprehensive-analysis
```

执行图：

```text
                     Requirement Skill
                            │
                            ▼
                    Context Skill
                            │
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼
        Data Skill      Knowledge Skill   Code Skill
             │              │              │
             └──────────────┼──────────────┘
                            ▼
                     Validation Skill
                            │
                    ┌───────┴────────┐
                    │                │
                  PASS             FAIL
                    │                │
                    ▼                ▼
             Report Skill       Retry / HITL
```

这个一个案例可以同时测：

```text
Skill → Skill

Skill并行

Skill条件分支

Skill → Tool

Skill → SubAgent

SubAgent → LangGraph

Checkpoint

HITL

Event

Run

Context

Skill Version
```

我认为它应该成为：

> **你们整个 Agent Platform 的 Golden Test Case。**

---

# 二十六、第一阶段不需要一下写 50 个案例

我建议真正开工先做 **10 个 P0 Case**：

| #  | 测试                                       |
| -- | ---------------------------------------- |
| 1  | **一个 Orchestrator Skill 顺序调用 3 个 Skill** |
| 2  | **Skill A 输出结构化结果给 Skill B**             |
| 3  | **Skill 中间失败 → interrupt → resume**      |
| 4  | **Skill → SubAgent → 结构化结果返回**           |
| 5  | **禁止 Skill/SubAgent 递归**                 |
| 6  | **同 Session 不同 Worker 连续执行**             |
| 7  | **Worker 执行中 kill → checkpoint 恢复**      |
| 8  | **两个用户同 Worker 数据完全隔离**                  |
| 9  | **Skill v1→v2，同 Run 锁版本、下一 Run 更新**      |
| 10 | **Tool Router：Local/Remote/Sandbox 可切换** |

这 10 个跑通以后，说明我们的基础架构不是“图画得好看”，而是真的成立。

然后第二批再加 Streaming、Cancel、Tool 幂等、并行 Skill、Context Compaction、Permission、Artifact 等。

其中我最建议你们**第一个就实现 `Orchestrator Skill → 多 Skill → Tool/SubAgent → 最终汇总`**。因为这个 Case 对 Deep Agents 的核心价值验证最大，也最容易暴露我们到底是在“真正编排 Skill”，还是只是把几段 Prompt 拼到了一起。
