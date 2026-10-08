# DCN 故障诊断前端

原生 HTML/CSS/JavaScript，无 npm 构建步骤。故障诊断连接项目后端的真实 SSE；隐患感知、深度诊断界面仍保留原来的模拟流程，本次未接入真实隐患服务。

## 启动

在项目根目录分别打开三个终端，均先执行 `conda activate agent`：

```bash
# 终端一：模拟 MCP，默认 8000 端口
python demo/demo_mcp_server.py
# 终端二：智能体后端，默认 8080 端口（需要 Ollama 已运行）
python run_server.py
# 终端三：前端静态服务与流式代理，默认 8090 端口
python frontend/app.py
```

浏览器打开 http://127.0.0.1:8090 。已有同端口服务时先关闭旧进程再启动，修改代码或配置后重启相应 Python 服务并刷新页面。

前端端口及后端地址在根目录 `config.toml` 的 `[frontend]` 中设置。浏览器与前端代理同源，不需要开放 CORS；可选后端令牌从运行前端的 `DCN_API_TOKEN` 环境变量读取，不写入浏览器。本版页面使用固定用户标识 local，适合当前本地联调，不是多用户登录系统。

## 诊断与流式事件

- 输入 POST `/v1/chat/stream`：`{"session_id":"dcn-...","message":"用户问题"}`。
- 前端生成唯一 session_id。同一会话追问继续用该 ID，新建对话生成新 ID。
- 展示 plan、reasoning（简短决策说明）、reflection、tool.call/result、answer.delta、context.compressed、clarification、error 等事件。
- 工具卡片按 call_id 配对，可独立展开/收缩入参和返回结果。长结果只展示后端片段，支持按 2000 字符翻页或打开完整 JSON。
- 停止按钮调用取消接口并中断浏览器请求；completed、needs_input、incomplete、timeout、error、cancelled、interrupted 分别显示。流结束但缺少终止事件不会显示“已完成”。
- 最近七个会话的已保存视图保存在当前浏览器 localStorage，刷新后可恢复并追问。它不是服务器会话列表；清除浏览器数据后侧栏记录会消失，后端 SQLite 历史仍在。刷新进行中的诊断会断开流，不自动重放工具。
- 用户上滚查看历史时，流式回答不会强制滚回底部。

## 拓扑与三个 MCP

页面通过后端 GET `/v1/topology?fabric_id=...` 加载背景拓扑。三个查询的工具名在根目录 `[topology]` 配置：

| 用途 | 演示工具名 | 入参 | 关联字段 |
| --- | --- | --- | --- |
| 全网 Fabric | list_fabrics | 无 | fabrics[].fabricId / nativeId / fabricName |
| 设备信息 | query_devices | pageSize | deviceInfo[].neResId / neName / neIp / neRole / neCategory |
| Fabric 拓扑 | query_fabric_topology | fabricId | nodes[].neResId / neName / NeIp；links 中的 src/des 资源 ID、名称、IP 和接口名 |

第三个 MCP 的入参按用途补为 `{"fabricId":"fabric-a"}`。真实服务名称不同时修改 `[topology]` 三个工具名及 `[mcp.servers]` 地址/允许名单；字段保持以上结构即可。设备列表目前只请求配置中的 device_page_size 条；如果真实服务需要额外分页协议，需提供该协议再接入，不能把前一页当成全网完整库存。

Pod 归属不在用户提供的原始字段中，模拟设备信息额外提供 podId/podName；前端优先显示 podName，其次 podId，缺失时明确显示“Pod 未提供”。不会将 Fabric 当作 Pod，也不会按设备名猜 Pod。

每次调用从业务参数或工具事件 location 中的设备名称、资源 ID、IP 定位设备，使用 neResId 关联拓扑。支持明确 fabricId 的自动切换；设备字段未提供 fabricId 时，按需查询其他 Fabric 的拓扑成员来确定归属并缓存，拓扑查询失败不终止诊断。蓝色表示当前排查；接口明确 oper_status=down 或 health_status=down/degraded 时标记异常设备及链路，模型文字中提到设备不会直接判定设备故障。artifact 检索保留上一设备定位。“范围”可选择自动跟随当前 Pod、指定 Pod 或全网概览。

模拟业务网含三个业务 Pod、双核心区、11 台设备、12 条链路，另有独立验证 Fabric。Leaf-03 / Ethernet1/49 为链路 down，Leaf-04 / Ethernet1/50 为 up 但 CRC 增长；完整演示覆盖技能、脚本、长日志、模拟修复和复查。使用 `python demo/run_walkthrough.py` 启动独立演示，详见 [demo/README.md](../demo/README.md)。DEMO 数据会在拓扑中标记。

推荐联调输入：

> 先发现 demo 服务的接口状态与日志查询工具，再查询 Leaf-03 的 Ethernet1/49 状态；如果 down，调用设备日志工具查询 Leaf-03 日志，再检索 loss_of_signal，给出证据，仅查询不修复。

模型仍由实际 Ollama 提供。小模型可能产生错误决策，页面会如实展示重试和失败，不用固定模拟结论覆盖。

## 界面样式修改

故障问答的字体、阶段标签、工具卡片和拓扑配色集中在 `static/diagnosis.css`，在公共样式之后加载。正文为 15px，过程说明为 13px，辅助标签为 11–12px。`static/diagnosis.js` 负责事件类型与展示结构；交换机图标由 `static/app.js` 中的 `appendDeviceIcon` 统一绘制，故障与隐患拓扑共用。修改静态文件后刷新页面即可生效。

## 隐患界面

`frontend/config.json` 保留原有 perception / deep_diagnosis 开关，默认开启。隐患数据、执行记录和深度诊断仍为旧演示；如暂不展示，可将对应开关设为 false 并刷新浏览器。
