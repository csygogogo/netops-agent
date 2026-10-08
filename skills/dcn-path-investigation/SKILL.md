---
name: dcn-path-investigation
description: 用于跨 Pod 业务主备路径故障排查，区分接口 down 与接口 up 但 CRC 持续增长。支持 DEMO 双故障全流程：状态查询、计数器增量脚本、长日志检索、模拟修复及业务复查。
---

# 跨 Pod 主备路径排查

1. 先读取 `references/procedure.md`，发现 MCP 的真实工具定义。
2. 演示任务先创建独立 case_id；后续查询与修复始终携带同一 ID。
3. 查询业务路径和业务状态，逐个查询主备接入接口。up 不等于健康。
4. 对错误增长接口获取两次计数器，用 `scripts/analyze_counters.py` 计算增量错误率，不用累计值直接判断。
5. 读取日志，再用 artifact.search 检索关键字，用 artifact.read 阅读命中附近内容。
6. 仅在用户授权时处置。DEMO 工具只模拟修复；真实服务必须依据其操作规范。
7. 修复后复查两端接口、计数器增量与业务主备路径。动作成功不等于业务恢复。
8. 报告证据、动作、验证和未完成事项。普通知识问答不必执行本流程。

演示输入与验收条件见 `references/procedure.md`。最终记录结构见 `assets/report-template.md`。
