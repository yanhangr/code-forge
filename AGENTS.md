# Code Forge 实现约定

- 开始工作先读 README.md、requirements.md、docs/detailed-design.md、docs/agent-workflow.md 和 docs/coding-standards.md；按 docs/implementation-guide.md 领取范围。
- 用户当前请求和已确认决策优先。docs/history/ 与旧评审仅作参考，不作为当前一期要求。
- 两个应用，Runtime优先；Python本机子进程；Skill手工维护；默认授权；管理产品和沙箱后置。不要扩大到完整Platform或改用付费Agent托管。
- 核心 contracts/state_machine/service/ports/audit 仅依赖标准库，技术适配器依赖核心。Platform只用公开HTTP/SSE契约。
- 公共状态、字段、事件、错误、数据库事务或恢复语义变化必须同步规范与测试；无法实现时先提出架构差异，不绕过约束制造成功结果。
- 自管PG表使用 date_created/created_by/date_updated/updated_by，表及字段有COMMENT；索引仅主键、普通和唯一的简单B-tree形式。
- 分支实现关联 docs/testing/agent-workflow-cases.json 的TC编号。区分核心测试、适配集成、框架链路、真实模型与HA验证。
- 修改契约后运行两个生成脚本和单元测试；当前交付核心架构，不声称完整Runtime/Platform已经可运行。
