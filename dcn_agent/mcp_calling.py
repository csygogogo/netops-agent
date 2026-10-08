"""通过 Streamable HTTP 调用 MCP 工具，供其他脚本直接 import。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _settings(url: str | None, headers: Mapping[str, str] | None, timeout: float):
    """读取服务地址与认证信息，并检查超时配置。"""
    url = url if url is not None else os.getenv("MCP_SERVER_URL", "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请传入 server_url 或设置 MCP_SERVER_URL（完整的 HTTP/HTTPS MCP 地址）")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须为有限正数（秒）")
    auth_headers = {}
    token = os.getenv("MCP_API_TOKEN")
    if token:
        auth_headers["Authorization"] = f"Bearer {token}"
    auth_headers = httpx.Headers(auth_headers)
    if headers:
        auth_headers.update(headers)
    return url, auth_headers


@asynccontextmanager
async def _session(url: str, headers: httpx.Headers, timeout: float) -> AsyncIterator[ClientSession]:
    """建立并初始化 MCP 会话，退出时释放 HTTP 连接。"""
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                async with asyncio.timeout(timeout):
                    await session.initialize()
                yield session


def _error_text(error: Exception) -> str:
    """展开嵌套异常，生成便于排查的错误说明。"""
    if isinstance(error, ExceptionGroup):
        return "; ".join(_error_text(e) for e in error.exceptions)
    return f"{type(error).__name__}: {error}"


async def multi_mcp_calling(
    calls: Sequence[Mapping[str, Any]], *, server_url: str | None = None,
    headers: Mapping[str, str] | None = None, timeout: float = 60.0,
    max_concurrency: int = 1,
) -> list[dict[str, Any]]:
    """批量调用同一 MCP 服务的工具，结果顺序与输入一致。

    输入：[{"name": "queryXX", "param": {"paraname1": "value1"}}]
    返回：[{"name": ..., "ok": bool, "text": ..., "data": ...,
           "content": [...], "error": str | None}]
    默认顺序执行；独立查询可增加 max_concurrency。不会自动重试。
    参数/配置错误抛 ValueError；工具/网络错误通过每项 ok=False 返回。
    单项失败继续其他项。取消信号向上传播，不转换为普通失败。
    """
    if isinstance(calls, (str, bytes, Mapping)) or not isinstance(calls, Sequence):
        raise ValueError("calls 必须是工具调用列表")
    normalized = []
    # 先校验整批输入，避免执行到一半才发现后续调用格式有误。
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping) or not isinstance(call.get("name"), str) or not call["name"].strip():
            raise ValueError(f"calls[{index}].name 必须为非空字符串")
        param = call.get("param", {})
        if not isinstance(param, dict):
            raise ValueError(f"calls[{index}].param 必须为字典")
        try:
            param = json.loads(json.dumps(param, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"calls[{index}].param 必须可序列化为 JSON") from exc
        normalized.append({"name": call["name"], "param": param})
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
        raise ValueError("max_concurrency 必须为正整数")
    if not normalized:
        return []
    url, auth_headers = _settings(server_url, headers, timeout)
    results: list[dict[str, Any] | None] = [None] * len(normalized)

    def failure(index: int, message: str) -> dict[str, Any]:
        return {"name": normalized[index]["name"], "ok": False, "text": "",
                "data": None, "content": [], "error": message}

    try:
        async with _session(url, auth_headers, timeout) as session:
            semaphore = asyncio.Semaphore(max_concurrency)

            async def invoke(index: int) -> None:
                async with semaphore:
                    call = normalized[index]
                    try:
                        async with asyncio.timeout(timeout):
                            result = await session.call_tool(call["name"], arguments=call["param"])
                        content = [block.model_dump(mode="json", by_alias=True, exclude_none=True)
                                   for block in result.content]
                        text = "\n".join(block["text"] for block in content if block.get("type") == "text")
                        results[index] = {
                            "name": call["name"], "ok": not result.isError,
                            "text": text, "data": result.structuredContent, "content": content,
                            "error": (text or "MCP 工具返回 isError=True") if result.isError else None,
                        }
                    except Exception as exc:
                        results[index] = failure(index, _error_text(exc))

            # TaskGroup 在退出 session 前回收所有调用，外部取消时不会遗留任务。
            async with asyncio.TaskGroup() as group:
                for index in range(len(normalized)):
                    group.create_task(invoke(index))
    except Exception as exc:
        # 保留已收到的结果；仅为没有完成的调用补充连接错误。
        for index, result in enumerate(results):
            if result is None:
                results[index] = failure(index, _error_text(exc))
    return results


async def list_mcp_tools(
    *, server_url: str | None = None, headers: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """返回工具名称、描述和 inputSchema；连接错误直接抛出。"""
    url, auth_headers = _settings(server_url, headers, timeout)
    tools = []
    async with _session(url, auth_headers, timeout) as session:
        cursor = None
        while True:
            async with asyncio.timeout(timeout):
                page = await session.list_tools(cursor=cursor)
            tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
    return tools


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=None)
    args = parser.parse_args()
    # 直接运行仅列出工具，不猜测或执行业务工具。
    print(json.dumps(asyncio.run(list_mcp_tools(server_url=args.server_url)), ensure_ascii=False, indent=2))
