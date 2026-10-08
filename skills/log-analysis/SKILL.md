---
name: log-analysis
description: 用于分析 MCP 返回的超长日志、单行日志和 JSON 日志，定位相关异常与前后证据，避免将头部截取误当作完整日志。
---

# 日志分析

先确定设备、时间范围、接口或错误代码。日志已返回时，使用其 artifact_id 进行检索。

1. 从具体设备/接口/错误码开始，调用 `artifact.search`，terms 是字面关键词列表。
2. 检索返回 `match_offset` 和片段 `offset`；用 `artifact.read` 读取更宽的前后文。
3. 分页时传入 `next_offset`，检查 `eof`，不能用第一页无结果推断整个文件无异常。
4. 单行超长内容同样按字符窗口读取，不依赖行号。
5. 记录时间、设备、异常内容和来源位置；不要将多次采样的累计计数误认为增量。
6. 如相关性不明确，更换检索词或扩大上下文，并明确结论的证据范围。

更多检索策略见 [references/search-guide.md](references/search-guide.md)。
