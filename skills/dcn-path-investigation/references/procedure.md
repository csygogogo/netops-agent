# DEMO 双故障流程

场景：checkout 业务从 POD-02 的双接入设备访问 POD-03 的 Leaf-05。Leaf-03 主接入链路 down，Leaf-04 备用接入链路 up 但 CRC 错误增长。三个 Pod 经两台 Core 互联。所有数据都是固定的模拟采样，不是实时生产数据。

1. `start_demo_case()` 创建 case_id。每次完整演示重新创建，不复用其他会话的实例。
2. `get_service_path(case_id, service="checkout")` 获取主备设备；`get_service_status(case_id)` 记录初始业务状态。
3. `get_interface_status(case_id, device="Leaf-03", interface="Ethernet1/49")`：预期 down、loss_of_signal。
4. `get_interface_status(case_id, device="Leaf-04", interface="Ethernet1/50")`：预期 up、health_status=degraded、crc_errors。
5. `get_interface_counters` 查询 Leaf-04 的该接口，将实际返回的 samples JSON 作为 `--samples` 参数传给技能脚本。`--threshold` 使用 `0.01`，表示演示阈值 1%。参数数组每项是单独字符串，不需要 shell 引号。

```json
{"name":"dcn-path-investigation","script":"scripts/analyze_counters.py","args":["--samples","[{\"time\":\"2026-09-18T10:00:00+08:00\",\"rx_packets\":100000,\"crc_errors\":100},{\"time\":\"2026-09-18T10:00:30+08:00\",\"rx_packets\":200000,\"crc_errors\":5100}]","--threshold","0.01"]}
```

示例数字只用于说明格式；执行时必须使用工具实际返回的计数。脚本预期得到 30 秒内接收包增量 100000、CRC 增量 5000、错误率 5%。计数回退、时间不递增或无流量时不可据此宣称健康。

6. `get_device_logs(case_id, device="Leaf-03")` 返回约 46 万字符的单行历史日志。记录返回的 artifact_id，搜索 `loss_of_signal`，再按命中 offset 读取附近 400 字符。不要搜索其他工具结果代替日志。
7. 用户授权模拟修复时，调用 `repair_demo_link`：Leaf-03 / Ethernet1/49 使用 action=`replace_optic`；Leaf-04 / Ethernet1/50 使用 action=`replace_cable`。动作只修改该 case_id，不会操作真实设备。
8. 分别重新查询 Leaf-03、Leaf-04 接口；再查询 Leaf-04 计数器并重新运行脚本，最后查询业务状态。

完成条件：两个接口均 healthy，CRC 增量错误率为 0%，业务 primary_healthy 和 backup_healthy 都是 true，1000 次模拟请求全部成功。主路径恢复后即使业务丢包归零，备用路径异常仍未闭环。历史日志保留旧错误，不应因此判定故障仍存在。

只读请求不得调用修复工具。当前能力不足、步骤预算不足或工具失败时明确未完成，遵循系统的补充信息开关。
