from __future__ import annotations

import asyncio
import os

from jsonschema import Draft202012Validator
from referencing import Registry

from .mcp_calling import list_mcp_tools, multi_mcp_calling
from .config import Settings
from .storage import encode


def schema(properties, required=()):
    """生成本地工具的参数说明，默认拒绝多余字段。"""
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 256}
PAGE = {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}

# 本地工具名称对应：用途说明、参数 schema。
SPECS = {
    "skill.list": ("查找技能元数据", schema({"query": {"type": "string", "maxLength": 256}, **PAGE})),
    "skill.load": ("按名称加载 SKILL.md", schema({"name": TEXT}, ["name"])),
    "skill.read": ("按需读取 references/、scripts/ 等文件", schema({"name": TEXT, "path": TEXT,
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 4000}}, ["name", "path"])),
    "skill.run": ("运行已信任技能的 scripts/*.py", schema({"name": TEXT, "script": TEXT,
        "args": {"type": "array", "items": {"type": "string", "maxLength": 4096}, "maxItems": 50}}, ["name", "script"])),
    "mcp.list_tools": ("分页发现 MCP 工具及参数 schema", schema({"server": TEXT,
        "query": {"type": "string", "maxLength": 256}, **PAGE}, ["server"])),
    "mcp.call": ("调用已发现的 MCP 工具", schema({"server": TEXT, "name": TEXT, "arguments": {"type": "object"}}, ["server", "name", "arguments"])),
    "artifact.search": ("在完整工具结果中搜索字面关键词，不依赖换行", schema({"artifact_id": TEXT,
        "terms": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 20},
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        "radius": {"type": "integer", "minimum": 0, "maximum": 500}}, ["artifact_id", "terms"])),
    "artifact.read": ("按字符 offset 分页读取完整结果", schema({"artifact_id": TEXT,
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 4000}}, ["artifact_id"])),
}


class Tools:
    """校验并分发本地技能、结果检索与远端 MCP 调用。"""
    def __init__(self, settings: Settings, skills, artifacts):
        self.settings = settings
        self.skills = skills
        self.artifacts = artifacts
        self.cache: dict[str, list] = {}  # 按服务名缓存真实工具定义

    def catalog(self):
        # 只注入简短参数说明，避免每轮携带所有远端 schema。
        """生成简短工具目录，远端完整参数定义按需发现。"""
        tools = []
        for name, (description, definition) in SPECS.items():
            if name == "skill.run" and not self.settings.skills.allow_scripts:
                continue
            tools.append({"name": name, "description": description,
                "arguments": {key: value["type"] + (" (required)" if key in definition["required"] else " (optional)")
                              for key, value in definition["properties"].items()}})
        return {"tools": tools, "mcp_servers": list(self.settings.mcp.servers),
                "skills": self.skills.list(limit=5)}

    def _server(self, name):
        """读取指定服务地址及专属令牌，避免跨服务混用。"""
        if name not in self.settings.mcp.servers:
            raise ValueError(f"未知 MCP 服务 {name}；当前服务：{list(self.settings.mcp.servers)}")
        config = self.settings.mcp.servers[name]
        token = os.getenv(config.token_env, "") if config.token_env else ""
        # 显式传入请求头，避免基础模块的全局令牌串到其他服务。
        return config, {"Authorization": f"Bearer {token}" if token else ""}

    async def _discover(self, server):
        """获取真实工具定义，应用允许名单并缓存。"""
        config, headers = self._server(server)
        tools = await list_mcp_tools(server_url=config.url, headers=headers, timeout=self.settings.agent.tool_timeout_seconds)
        if config.allowed_tools:
            tools = [t for t in tools if t["name"] in config.allowed_tools]
        self.cache[server] = tools
        return tools

    async def call(self, name: str, arguments: dict, session_id: str, loaded: list[str], run_id: str):
        """先检查参数，再执行对应工具；结果文件按会话隔离。"""
        if name not in SPECS:
            raise ValueError(f"未知工具: {name}")
        Draft202012Validator(SPECS[name][1]).validate(arguments)
        if name == "skill.list":
            return self.skills.list(**arguments)
        if name == "skill.load":
            result = await asyncio.to_thread(self.skills.load, **arguments)
            skill = arguments["name"]
            if skill in loaded:
                loaded.remove(skill)
            loaded.append(skill)
            del loaded[:-self.settings.skills.max_active]
            return result
        if name in {"skill.read", "skill.run"}:
            if arguments["name"] not in loaded:
                raise ValueError("请先 skill.load 激活该技能")
            if name == "skill.read":
                return await asyncio.to_thread(self.skills.read, **arguments)
            saved = self.artifacts.save(session_id, run_id, "skill.run", {})
            path = self.artifacts.raw_path(session_id, saved["artifact_id"]).with_suffix(".txt")
            path.write_bytes(b"")
            result = {"ok": False, "status": "cancelled", "exit_code": None}
            try:
                result = await self.skills.run(args=arguments.get("args", []), name=arguments["name"],
                    script=arguments["script"], output_path=path)
            except Exception as exc:
                result = {"ok": False, "status": "error", "exit_code": None, "error": str(exc)}
            finally:
                # 超时或取消也保留已落盘的输出，供后续按字符检索。
                await asyncio.to_thread(self.artifacts.finish_output, session_id, saved["artifact_id"], result)
            return {**result, "artifact_id": saved["artifact_id"]}
        if name == "mcp.list_tools":
            server = arguments["server"]
            tools = await self._discover(server)
            query = arguments.get("query", "").lower()
            tools = [t for t in tools if query in (t["name"] + (t.get("description") or "")).lower()]
            start, limit = arguments.get("offset", 0), arguments.get("limit", 5)
            return {"server": server, "tools": tools[start:start + limit], "total": len(tools), "next_offset": min(start + limit, len(tools))}
        if name == "mcp.call":
            server, tool = arguments["server"], arguments["name"]
            config, headers = self._server(server)
            available = self.cache.get(server)
            if available is None:
                available = await self._discover(server)
            definition = next((t for t in available if t["name"] == tool), None)
            if definition is None:
                return {"ok": False, "error": "工具不存在或不在 allowed_tools 中；请根据真实目录调用",
                        "available_tools": available[:5], "total": len(available),
                        "hint": "调用 mcp.list_tools 可按 query 筛选或 offset 翻页"}
            # 禁止 schema 自动读取外部引用 URL。
            Draft202012Validator(definition["inputSchema"], registry=Registry()).validate(arguments["arguments"])
            result = await multi_mcp_calling([{"name": tool, "param": arguments["arguments"]}],
                server_url=config.url, headers=headers, timeout=self.settings.agent.tool_timeout_seconds)
            return result[0]
        if name == "artifact.search":
            return await asyncio.to_thread(self.artifacts.search, session_id=session_id, **arguments)
        if name == "artifact.read":
            return await asyncio.to_thread(self.artifacts.read, session_id=session_id, **arguments)
        raise ValueError("未实现的工具")
