"""Ollama 的 OpenAI 兼容接口：普通响应和流式文本，同步/异步均可用。"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from openai import AsyncOpenAI, OpenAI


def _client_options(base_url: str | None, api_key: str | None, timeout: float) -> dict:
    """组合连接配置，禁用自动重试以免重复生成。"""
    return {
        "base_url": base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": api_key or os.getenv("OLLAMA_API_KEY", "ollama"),
        "timeout": timeout,
        "max_retries": 0,
    }


def _request(messages: str | Sequence[dict[str, Any]], model: str | None, options: dict) -> dict:
    """将字符串或消息列表整理为统一请求参数。"""
    if "stream" in options:
        raise ValueError("请使用 stream_llm / astream_llm 开启流式输出")
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if not messages:
        raise ValueError("messages 不能为空")
    return {"model": model or os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
            "messages": list(messages), **options}


def call_llm(
    messages: str | Sequence[dict[str, Any]], *, model: str | None = None,
    base_url: str | None = None, api_key: str | None = None,
    timeout: float = 120.0, **options: Any,
) -> str:
    """返回完整文本；options 可传 temperature、max_tokens 等兼容参数。"""
    request = _request(messages, model, options)
    with OpenAI(**_client_options(base_url, api_key, timeout)) as client:
        response = client.chat.completions.create(**request, stream=False)
        return response.choices[0].message.content or ""


def stream_llm(
    messages: str | Sequence[dict[str, Any]], *, model: str | None = None,
    base_url: str | None = None, api_key: str | None = None,
    timeout: float = 120.0, **options: Any,
) -> Iterator[str]:
    """逐块 yield 文本；打印由调用方负责。提前停止时请 close() 生成器。"""
    request = _request(messages, model, options)
    finished = False
    with OpenAI(**_client_options(base_url, api_key, timeout)) as client:
        with client.chat.completions.create(**request, stream=True) as stream:
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                if chunk.choices and chunk.choices[0].finish_reason:
                    _check_finish(chunk.choices[0].finish_reason)
                    finished = True
    if not finished:
        raise RuntimeError("模型流中断：未收到 finish_reason")


async def acall_llm(
    messages: str | Sequence[dict[str, Any]], *, model: str | None = None,
    base_url: str | None = None, api_key: str | None = None,
    timeout: float = 120.0, **options: Any,
) -> str:
    """异步返回完整文本，不阻塞 MCP 所在的事件循环。"""
    request = _request(messages, model, options)
    async with AsyncOpenAI(**_client_options(base_url, api_key, timeout)) as client:
        response = await client.chat.completions.create(**request, stream=False)
        return response.choices[0].message.content or ""


async def astream_llm(
    messages: str | Sequence[dict[str, Any]], *, model: str | None = None,
    base_url: str | None = None, api_key: str | None = None,
    timeout: float = 120.0, **options: Any,
) -> AsyncIterator[str]:
    """异步逐块 yield 文本；提前停止时请 aclose() 生成器。"""
    request = _request(messages, model, options)
    finished = False
    async with AsyncOpenAI(**_client_options(base_url, api_key, timeout)) as client:
        stream = await client.chat.completions.create(**request, stream=True)
        async with stream:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                if chunk.choices and chunk.choices[0].finish_reason:
                    _check_finish(chunk.choices[0].finish_reason)
                    finished = True
    if not finished:
        raise RuntimeError("模型流中断：未收到 finish_reason")


def _check_finish(reason: str):
    """文本调用只有 stop 算正常完成，截断或拒绝不能伪装成完整回答。"""
    if reason != "stop":
        raise ValueError(f"模型输出未正常完成：finish_reason={reason}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="test")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    if args.no_stream:
        print(call_llm(args.prompt, model=args.model, base_url=args.base_url))
    else:
        from contextlib import closing
        with closing(stream_llm(args.prompt, model=args.model, base_url=args.base_url)) as chunks:
            for text in chunks:
                print(text, end="", flush=True)
        print()
