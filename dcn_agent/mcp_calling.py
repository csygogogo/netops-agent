"""调用 MCP 工具：Streamable HTTP 服务与本地子进程 stdio 脚本，供其他脚本直接 import。"""

from __future__ import annotations

import argparse
import asyncio
from async_timeout import timeout as async_timeout
import io
import json
import math
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from anyio import create_task_group
try:
    from builtins import ExceptionGroup
except ImportError:
    from exceptiongroup import ExceptionGroup
from mcp import ClientSession, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import stdio_client
try:  # mcp 2.x 分页参数对象；1.x 没有，使用 cursor 关键字
    from mcp.types import PaginatedRequestParams
except ImportError:
    PaginatedRequestParams = None


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
        async with streamable_http_client(url, http_client=http) as streams:
            # mcp 1.x 返回 (read, write, get_session_id)，2.x 返回 (read, write)；按下标取前两个。
            read, write = streams[0], streams[1]
            # 不传 read_timeout_seconds：mcp 1.30 只接受 timedelta、2.x 只接受 float 秒，
            # 类型冲突无法在构造期探测（1.30 运行时才调 .total_seconds() 报错）；
            # None 语义两版一致（不启用库内读超时），超时由外层 async_timeout 统一控制。
            async with ClientSession(read, write) as session:
                async with async_timeout(timeout):
                    await session.initialize()
                yield session


@asynccontextmanager
async def _stdio_session(script: str, python: str | None, timeout: float) -> AsyncIterator[ClientSession]:
    """以子进程 stdio 方式启动本地 MCP 脚本并初始化会话，退出时结束子进程。

    客户端容忍脚本在 stdout 的杂散输出（如调试 print），只解析 JSON-RPC 行。
    """
    # 用真实临时文件收集脚本 stderr（子进程重定向需要文件描述符）；
    # 脚本崩溃退出时把根因 traceback 带给调用方。
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errlog:
        params = StdioServerParameters(command=python or sys.executable, args=[script])
        try:
            # errlog 是 stdio_client 的参数（mcp 1.x/2.x 相同）。
            async with stdio_client(params, errlog=errlog) as (read, write):
                # read_timeout_seconds 不传的原因见 _session 内注释。
                async with ClientSession(read, write) as session:
                    async with async_timeout(timeout):
                        await session.initialize()
                    yield session
        except Exception as exc:
            errlog.seek(0)
            tail = errlog.read()[-4000:].strip()
            if tail:
                raise RuntimeError(f"{error_text(exc)}；脚本 stderr 尾部：\n{tail}") from exc
            raise


async def _list_all_tools(session: ClientSession, timeout: float) -> list[dict[str, Any]]:
    """分页列出全部工具；兼容 mcp 1.x（cursor 参数）与 2.x（params 对象）。"""
    tools: list[dict[str, Any]] = []
    cursor = None
    while True:
        async with async_timeout(timeout):
            if PaginatedRequestParams is None:
                page = await session.list_tools(cursor=cursor)
            else:
                page = await session.list_tools(
                    params=None if cursor is None else PaginatedRequestParams(cursor=cursor))
        tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in page.tools)
        cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)  # 2.x/1.x 属性名不同
        if not cursor:
            return tools


def error_text(error: Exception) -> str:
    """展开嵌套异常，生成便于排查的错误说明。"""
    if isinstance(error, ExceptionGroup):
        return "; ".join(error_text(e) for e in error.exceptions)
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
    normalized = _normalize_calls(calls)
    _check_concurrency(max_concurrency)
    if not normalized:
        return []
    url, auth_headers = _settings(server_url, headers, timeout)
    async with _session(url, auth_headers, timeout) as session:
        return await _dispatch(session, normalized, timeout, max_concurrency)


async def multi_stdio_calling(
    calls: Sequence[Mapping[str, Any]], *, script: str,
    python: str | None = None, timeout: float = 60.0,
    max_concurrency: int = 1,
) -> list[dict[str, Any]]:
    """stdio 版批量调用：每次调用启动一个脚本子进程会话。

    输入、返回结构、错误语义与 multi_mcp_calling 完全一致；script 是本地
    MCP 脚本路径（FastMCP 编写、mcp.run() 启动），python 留空使用当前解释器。
    """
    if isinstance(calls, (str, bytes, Mapping)) or not isinstance(calls, Sequence):
        raise ValueError("calls 必须是工具调用列表")
    normalized = _normalize_calls(calls)
    _check_concurrency(max_concurrency)
    if not normalized:
        return []
    async with _stdio_session(script, python, timeout) as session:
        return await _dispatch(session, normalized, timeout, max_concurrency)


def _normalize_calls(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """先校验整批输入，避免执行到一半才发现后续调用格式有误。"""
    normalized = []
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
    return normalized


def _check_concurrency(max_concurrency: int):
    """并发参数只接受正整数。"""
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
        raise ValueError("max_concurrency 必须为正整数")


async def _dispatch(session: ClientSession, normalized: list[dict], timeout: float,
                    max_concurrency: int) -> list[dict[str, Any]]:
    """在已建立的会话上并发执行整批调用；单项失败继续其他项。"""
    results: list[dict[str, Any] | None] = [None] * len(normalized)

    def failure(index: int, message: str) -> dict[str, Any]:
        return {"name": normalized[index]["name"], "ok": False, "text": "",
                "data": None, "content": [], "error": message}

    try:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def invoke(index: int) -> None:
            async with semaphore:
                call = normalized[index]
                try:
                    async with async_timeout(timeout):
                        result = await session.call_tool(call["name"], arguments=call["param"])
                    content = [block.model_dump(mode="json", by_alias=True, exclude_none=True)
                               for block in result.content]
                    text = "\n".join(block["text"] for block in content if block.get("type") == "text")
                    # mcp 1.x 属性为 camelCase（isError），2.x 为 snake_case（is_error）。
                    is_error = getattr(result, "isError", getattr(result, "is_error", False))
                    data = getattr(result, "structuredContent",
                                   getattr(result, "structured_content", None))
                    results[index] = {
                        "name": call["name"], "ok": not is_error,
                        "text": text, "data": data, "content": content,
                        "error": (text or "MCP 工具返回 isError=True") if is_error else None,
                    }
                except Exception as exc:
                    results[index] = failure(index, error_text(exc))

        # TaskGroup 在退出 session 前回收所有调用，外部取消时不会遗留任务。
        async with create_task_group() as group:
            for index in range(len(normalized)):
                group.start_soon(invoke, index)
    except Exception as exc:
        # 保留已收到的结果；仅为没有完成的调用补充连接错误。
        for index, result in enumerate(results):
            if result is None:
                results[index] = failure(index, error_text(exc))
    return results


async def list_mcp_tools(
    *, server_url: str | None = None, headers: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """返回工具名称、描述和 inputSchema；连接错误直接抛出。"""
    url, auth_headers = _settings(server_url, headers, timeout)
    async with _session(url, auth_headers, timeout) as session:
        return await _list_all_tools(session, timeout)


async def list_stdio_tools(
    *, script: str, python: str | None = None, timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """列出 stdio 脚本的名称、描述和 inputSchema；连接错误直接抛出。"""
    async with _stdio_session(script, python, timeout) as session:
        return await _list_all_tools(session, timeout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--script", default=None, help="本地 stdio MCP 脚本路径")
    args = parser.parse_args()
    # 直接运行仅列出工具，不猜测或执行业务工具。
    tools = asyncio.run(list_stdio_tools(script=args.script) if args.script
                        else list_mcp_tools(server_url=args.server_url))
    print(json.dumps(tools, ensure_ascii=False, indent=2))
