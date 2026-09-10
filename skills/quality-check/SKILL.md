---
name: quality-check
description: 核验生成文件和结果的一致性。
version: "1"
---

# Quality Check

1. 读取最终文件和中间文件。
2. 用 execute_python 或 execute_command 核验文件存在、可读、内容非空。
3. 发现不一致时给出明确失败证据，不得仅凭模型判断通过。
