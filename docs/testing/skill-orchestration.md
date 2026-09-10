# Skill 编排验证表

日期：2026-09-10。当前先验证“单个 Skill 编排多个 Skill”的能力，不把未运行的模型选择、跨轮升级或上下文清理标记为已完成。

| 用例/分支 | 能力 | 验证级别 | 当前结果 | 证据 |
| --- | --- | --- | --- | --- |
| B06 / 快照解析失败 | 缺失依赖、循环、版本不兼容必须拒绝 | L1 | 已验证缺失依赖与循环拒绝 | `tests/test_skill_orchestration.py` |
| B16 / Skill 激活与非法候选 | 根 Skill 自动解析并冻结其依赖 Skill | L1 | 已验证传递解析、去重、不可变快照 | `tests/test_skill_orchestration.py::test_one_skill_resolves_multiple_nested_skills` |
| B45 / 新轮 Skill 升级 | 修改源 Skill 后，新 Run 使用新快照，旧 Run 保留旧快照 | L1 | 已由现有本地测试部分覆盖 | `tests/test_runtime_local.py::test_session_and_skill_snapshot_are_immutable` |
| 模型按需激活 Skill | 模型只从冻结候选中选择 Skill | L2+L3 | 未验证 | 待真实模型上下文选择链路 |
| Skill 正文/摘要跨轮清理 | 升级后旧正文不继续作为活动指令 | L2 | 未验证 | 待上下文管理/压缩链路 |

## Skill 编排语义

手工 Skill 的 `SKILL.md` frontmatter 可声明：

```markdown
---
name: analysis-report
version: "1"
requires: ["python-analysis", "file-writer"]
---
```

`ManualSkillResolver` 会递归解析依赖，计算每个包的 digest，并把所有传递 Skill 的不可变 bundle 写入 `RunSnapshot`。空 `skills` 列表仍不自动加载全部 Skill。
