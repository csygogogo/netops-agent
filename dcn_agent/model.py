from contextlib import aclosing
import json
import os
import httpx

from .llm_calling import astream_llm

from .budget import message_tokens
from .config import ModelConfig


class ContextOverflow(ValueError):
    """输入和预留输出超出配置窗口时抛出。"""
    pass


class Model:
    """统一原生 Ollama 与 OpenAI 兼容接口的异步文本输出。"""
    def __init__(self, config: ModelConfig):
        self.config = config

    async def stream(self, messages: list[dict], *, json_mode: bool | dict = False, max_tokens: int | None = None):
        """检查预算并逐块读取模型文本，同时检查流是否正常结束。"""
        output = max_tokens or self.config.max_output_tokens  # 本次输出预留预算
        if message_tokens(messages) + output + self.config.safety_margin_tokens > self.config.context_window_tokens:
            raise ContextOverflow("模型请求超过配置的上下文预算")
        if self.config.transport == "ollama":
            url = self.config.base_url.rstrip("/").removesuffix("/v1") + "/api/chat"
            payload = {"model": self.config.model, "messages": messages, "stream": True,
                "options": {"num_ctx": self.config.context_window_tokens, "num_predict": output,
                            "temperature": self.config.temperature}}
            if json_mode:
                payload["format"] = json_mode if isinstance(json_mode, dict) else "json"
            headers = {}
            if os.getenv(self.config.api_key_env):
                headers["Authorization"] = "Bearer " + os.environ[self.config.api_key_env]
            done = False  # 必须收到服务端完成标记才算正常结束
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds, headers=headers) as client:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise RuntimeError(str(chunk["error"]))
                        text = chunk.get("message", {}).get("content", "")
                        if text:
                            yield text
                        if chunk.get("done"):
                            done = True
                            if chunk.get("done_reason") == "length":
                                raise ValueError("模型达到输出长度限制，输出可能不完整")
            if not done:
                raise RuntimeError("Ollama 流中断：未收到 done 标记")
            return
        options = {"max_tokens": output, "temperature": self.config.temperature}
        if json_mode:
            options["response_format"] = ({"type": "json_schema", "json_schema": {
                "name": "agent_decision", "strict": True, "schema": json_mode,
            }} if isinstance(json_mode, dict) else {"type": "json_object"})
        async with aclosing(astream_llm(
            messages, model=self.config.model, base_url=self.config.base_url,
            api_key=os.getenv(self.config.api_key_env, "ollama"),
            timeout=self.config.timeout_seconds, **options,
        )) as chunks:
            async for text in chunks:
                yield text

    async def complete(self, messages, *, json_mode=False, max_tokens=None):
        """收集流式文本，用于结构化决策和摘要生成。"""
        chunks = []
        async with aclosing(self.stream(messages, json_mode=json_mode, max_tokens=max_tokens)) as stream:
            async for text in stream:
                chunks.append(text)
        return "".join(chunks)
