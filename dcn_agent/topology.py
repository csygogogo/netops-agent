"""通过配置中的三个只读 MCP 获取拓扑，保留服务端字段供前端展示。"""
import asyncio
import json


async def snapshot(tools, config, fabric_id: str = ""):
    """设备归属以 neResId 关联；Pod 只能来自服务字段，不从 Fabric 推断。"""
    async def query(name, arguments):
        result = await tools.call("mcp.call", {"server": config.server, "name": name,
            "arguments": arguments}, "", [], "")
        if not result.get("ok"):
            raise ValueError(result.get("error") or f"{name} 查询失败")
        data = result.get("data") or json.loads(result.get("text") or "{}")
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if not isinstance(data, dict) or data.get("status_code", 200) != 200:
            raise ValueError(f"{name} 返回无效数据或失败状态")
        return data

    fabrics, devices = await asyncio.gather(query(config.fabric_tool, {}),
        query(config.device_tool, {"pageSize": config.device_page_size}))
    items = fabrics.get("fabrics", [])
    selected = fabric_id or (str(items[0]["fabricId"]) if items else "")
    if selected and not any(str(f["fabricId"]) == selected for f in items):
        raise ValueError("Fabric 不在服务返回的列表中")
    topology = await query(config.topology_tool, {"fabricId": selected}) if selected else {"nodes": [], "links": []}
    return {"fabrics": items, "deviceInfo": devices.get("deviceInfo", []),
            "fabricId": selected, "nodes": topology.get("nodes", []), "links": topology.get("links", []),
            "data_source": topology.get("data_source", "MCP"),
            "device_page_size": config.device_page_size}
