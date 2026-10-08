"""多 Pod 双故障模拟 MCP；不会访问真实设备，修复仅作用于独立演示实例。"""
from uuid import uuid4
from typing import Literal
from mcp.server.fastmcp import FastMCP

server = FastMCP("DCN Demo", host="127.0.0.1", port=8000, json_response=True)
FABRICS = [
    {"fabricId": "fabric-a", "nativeId": "native-a", "fabricName": "DEMO · 多 Pod 业务网"},
    {"fabricId": "fabric-b", "nativeId": "native-b", "fabricName": "DEMO · 独立验证网"},
]
# 保留原有六台设备的资源 ID；Pod 是明确提供的演示字段。
DEVICES = [
    {"neResId": f"ne-{i:02}", "neName": name, "neIp": f"10.0.0.{i}",
     "neCategory": "Switch", "neRole": role, "fabricId": fabric, "podId": pod, "podName": pod.upper()}
    for i, (name, role, fabric, pod) in enumerate([
        ("Spine-01", "Spine", "fabric-a", "pod-01"),
        ("Spine-02", "Spine", "fabric-a", "pod-02"),
        ("Leaf-01", "Leaf", "fabric-a", "pod-01"),
        ("Leaf-03", "Leaf", "fabric-a", "pod-02"),
        ("Spine-11", "Spine", "fabric-b", "pod-11"),
        ("Leaf-11", "Leaf", "fabric-b", "pod-11"),
        ("Leaf-02", "Leaf", "fabric-a", "pod-01"),
        ("Leaf-04", "Leaf", "fabric-a", "pod-02"),
        ("Spine-03", "Spine", "fabric-a", "pod-03"),
        ("Leaf-05", "Leaf", "fabric-a", "pod-03"),
        ("Leaf-06", "Leaf", "fabric-a", "pod-03"),
        ("Core-01", "Core", "fabric-a", "core"),
        ("Core-02", "Core", "fabric-a", "core"),
    ], 1)
]
FAULTS = {("Leaf-03", "Ethernet1/49"): "replace_optic", ("Leaf-04", "Ethernet1/50"): "replace_cable"}
CASES = {}  # case_id -> 已修复接口集合，不同演示实例互不影响。


def find_device(device):
    """接受设备名、资源 ID 或 IP，未知设备不伪造结果。"""
    for item in DEVICES:
        if device.lower() in {str(item[k]).lower() for k in ("neName", "neResId", "neIp")}:
            return item
    raise ValueError("模拟设备不存在，请先 query_devices")


def make_link(source, source_port, target, target_port):
    """按拓扑协议生成设备和接口关联。"""
    a, b = find_device(source), find_device(target)
    return {"srcNeResId": a["neResId"], "srcNeName": source, "srcNeIp": a["neIp"], "srcItfName": source_port,
            "desNeResId": b["neResId"], "desNeName": target, "desNeIp": b["neIp"], "desItfName": target_port}


LINKS = [make_link(f"Spine-{pod:02}", f"Ethernet1/{i}", f"Leaf-{(pod-1)*2+i:02}",
                  "Ethernet1/50" if (pod, i) == (2, 2) else "Ethernet1/49")
         for pod in range(1, 4) for i in (1, 2)]
LINKS += [make_link(f"Core-{core:02}", f"Ethernet1/{pod}", f"Spine-{pod:02}", f"Ethernet1/{48+core}")
          for core in (1, 2) for pod in range(1, 4)]
LINKS += [make_link("Spine-11", "Ethernet1/1", "Leaf-11", "Ethernet1/49")]


def repaired(case_id):
    """空 ID 查询只读基线；有效 ID 查询独立实例。"""
    if not case_id:
        return set()
    if case_id not in CASES:
        raise ValueError("实例不存在，请先 start_demo_case；重启服务后需重新创建")
    return CASES[case_id]


@server.tool()
def list_fabrics() -> dict:
    """全网 Fabric 列表查询。"""
    return {"status_code": 200, "fabrics": FABRICS, "data_source": "DEMO"}


@server.tool()
def query_devices(pageSize: int) -> dict:
    """查询最多 pageSize 台设备。"""
    if not 1 <= pageSize <= 10000:
        raise ValueError("pageSize 范围 1..10000")
    return {"status_code": 200, "deviceInfo": DEVICES[:pageSize], "data_source": "DEMO"}


@server.tool()
def query_fabric_topology(fabricId: str) -> dict:
    """查询 Fabric 物理拓扑，包含跨 Pod 的核心链路。"""
    if fabricId not in {f["fabricId"] for f in FABRICS}:
        raise ValueError("未知 fabricId")
    nodes = [{"neResId": d["neResId"], "neName": d["neName"], "NeIp": d["neIp"]}
             for d in DEVICES if d["fabricId"] == fabricId]
    ids = {n["neResId"] for n in nodes}
    return {"nodes": nodes, "links": [e for e in LINKS if e["srcNeResId"] in ids], "data_source": "DEMO"}


@server.tool()
def start_demo_case() -> dict:
    """创建双故障演示实例；后续所有查询、模拟修复携带返回的 case_id。"""
    case_id = "demo-" + uuid4().hex[:12]
    CASES[case_id] = set()
    return {"case_id": case_id, "service": "checkout", "fabricId": "fabric-a", "data_source": "DEMO",
            "symptom": "POD-02 双接入订单服务访问 POD-03 数据服务，主路径不可用，备用路径约 5% 丢包"}


@server.tool()
def get_service_path(case_id: str, service: str = "checkout") -> dict:
    """查询业务主备路径及接入接口，不直接给出故障根因。"""
    repaired(case_id)
    if service != "checkout":
        raise ValueError("演示仅提供 checkout 服务")
    return {"case_id": case_id, "service": service, "fabricId": "fabric-a", "data_source": "DEMO",
            "primary": ["Leaf-03", "Spine-02", "Core-01", "Spine-03", "Leaf-05"],
            "backup": ["Leaf-04", "Spine-02", "Core-02", "Spine-03", "Leaf-05"],
            "access_interfaces": [{"device": "Leaf-03", "interface": "Ethernet1/49"},
                                  {"device": "Leaf-04", "interface": "Ethernet1/50"}]}


@server.tool()
def get_interface_status(device: str, interface: str, case_id: str = "") -> dict:
    """接口状态；degraded 表示 up 但存在错误；不指定 case_id 时查询只读故障基线。"""
    item = find_device(device)
    edge = next((e for e in LINKS if (e["srcNeName"], e["srcItfName"]) == (item["neName"], interface)
                 or (e["desNeName"], e["desItfName"]) == (item["neName"], interface)), None)
    if not edge:
        raise ValueError("模拟接口不存在，请先查询拓扑")
    fault = (edge["desNeName"], edge["desItfName"])
    active = fault in FAULTS and fault not in repaired(case_id)
    down = active and fault[0] == "Leaf-03"
    return {**item, "device": item["neName"], "interface": interface, "admin_status": "up",
            "oper_status": "down" if down else "up", "health_status": "down" if down else "degraded" if active else "healthy",
            "reason": "loss_of_signal" if down else "crc_errors" if active else "no_current_alarm",
            "case_id": case_id or "baseline", "data_source": "DEMO"}


@server.tool()
def get_interface_counters(case_id: str, device: str, interface: str) -> dict:
    """返回相隔 30 秒的累计接收包和 CRC 计数，需要脚本计算增量错误率。"""
    state = get_interface_status(device, interface, case_id)
    errors = 5000 if state["health_status"] == "degraded" else 0
    # 修复后的验证使用后续采样窗口，累计计数不倒退。
    later = bool(repaired(case_id))
    minute, packets, crc = ("01", 300000, 5100) if later else ("00", 100000, 100)
    return {"case_id": case_id, "device": state["device"], "interface": interface, "data_source": "DEMO",
            "samples": [{"time": f"2026-09-18T10:{minute}:00+08:00", "rx_packets": packets, "crc_errors": crc},
                        {"time": f"2026-09-18T10:{minute}:30+08:00", "rx_packets": packets + 100000, "crc_errors": crc + errors}]}


@server.tool()
def get_device_logs(device: str, case_id: str = "") -> str:
    """超长单行历史日志，使用 artifact.search/read 检索；旧错误不等于当前状态。"""
    item = find_device(device)
    fixed = repaired(case_id)
    evidence = {"Leaf-03": "interface=Ethernet1/49 reason=loss_of_signal rx_power_dbm=-35",
                "Leaf-04": "interface=Ethernet1/50 reason=crc_errors crc_delta=5000"}.get(item["neName"])
    alarm = f"10:00:05 ERROR {evidence}" if evidence else "10:00:05 INFO no_alarm"
    recovery = "10:01:00 INFO link_recovered" if any(d == item["neName"] for d, _ in fixed) else "10:00:30 INFO sample_end"
    return "DEMO historical log;" + "INFO heartbeat healthy;" * 10000 + f" device={item['neName']} {alarm}; {recovery};" + "INFO collector healthy;" * 10000


@server.tool()
def repair_demo_link(case_id: str, device: str, interface: str, action: Literal["replace_optic", "replace_cable"]) -> dict:
    """仅修改 DEMO 实例：Leaf-03 模拟更换光模块，Leaf-04 模拟更换线缆。无真实设备操作。"""
    target = (find_device(device)["neName"], interface)
    if FAULTS.get(target) != action or not case_id:
        raise ValueError("需提供有效 case_id，且修复动作必须匹配演示故障接口")
    fixed = repaired(case_id)
    already = target in fixed
    fixed.add(target)
    return {"case_id": case_id, "device": target[0], "interface": interface, "applied": True,
            "already_applied": already, "action": action, "data_source": "DEMO", "hint": "重新查询接口、计数器及业务状态验证"}


@server.tool()
def get_service_status(case_id: str) -> dict:
    """验证业务和主备路径；两条路径健康才算完整恢复。"""
    fixed = repaired(case_id)
    primary = ("Leaf-03", "Ethernet1/49") in fixed
    backup = ("Leaf-04", "Ethernet1/50") in fixed
    loss = 0 if primary or backup else 5
    return {"case_id": case_id, "service": "checkout", "primary_healthy": primary, "backup_healthy": backup,
            "status": "healthy" if primary and backup else "degraded", "loss_percent": loss,
            "requests": 1000, "successes": 1000 - loss * 10, "data_source": "DEMO"}


if __name__ == "__main__":
    server.run(transport="streamable-http")
