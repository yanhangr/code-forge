---
name: data-report
description: 编排数据生成、分析、写入和质量核验。
version: "1"
requires: ["data-extract", "python-analysis", "file-writer", "quality-check"]
---

# Data Report

1. 使用 data-extract 生成 `data.csv`。
2. 使用 python-analysis 计算平方和、平均值、最大值和最小值，保存到 `analysis.json`。
3. 使用 file-writer 生成最终 `report.md`。
4. 使用 quality-check 核验 `data.csv`、`analysis.json` 和 `report.md` 都存在且可读。
5. 最终回复按阶段列出每个 Skill 的执行结果和证据。
