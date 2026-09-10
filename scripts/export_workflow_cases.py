"""Render reviewed test specifications; never marks an unexecuted case as passed."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(registry: dict) -> str:
    lines = [
        "# Agent 流程分支测试用例",
        "",
        "来源：[JSON 注册表](agent-workflow-cases.json)。流程：[完整工作流程](../agent-workflow.md)。",
        "",
        "状态 SPEC_ONLY 表示完整用例尚待实现/执行。core_evidence 只证明其中部分纯核心规则，不能作为集成通过记录。L0 为核心单元，L1 为数据库/执行/接口集成，L2 为真实框架链路，L3 为真实模型任务。",
        "",
        "| 用例 | 流程分支 | 名称 | 验证级别 | 当前状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in registry["cases"]:
        lines.append(
            f"| {case['case_id']} | {case['flow_id']} / {case['branch_id']} | {case['title']} | {case['level']} | {case['status']} |"
        )
    for case in registry["cases"]:
        lines.extend(
            [
                "",
                f"## {case['case_id']}：{case['title']}",
                "",
                f"对应分支：{case['flow_id']} / {case['branch_id']}；验证级别：{case['level']}；状态：{case['status']}。",
                "",
            ]
        )
        for key, label in [("given", "前置条件"), ("when", "操作/故障注入"), ("then", "断言")]:
            lines.append(f"**{label}**")
            lines.append("")
            lines.extend(f"- {item}" for item in case[key])
            lines.append("")
        lines.append(f"计划自动化位置：`{case['planned_test']}`（尚未建立）。")
        if case["core_evidence"]:
            lines.extend(["", "已存在的部分核心证据："])
            for ref in case["core_evidence"]:
                file, selector = ref.split("::", 1)
                lines.append(f"- [{selector}](../../{file})")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    source = json.loads((ROOT / "docs/testing/agent-workflow-cases.json").read_text())
    (ROOT / "docs/testing/agent-workflow-cases.md").write_text(render(source))
