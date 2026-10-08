"""通过 POST 返回 SSE；有界队列控制积压，并在空闲时发送心跳。"""
from __future__ import annotations

import asyncio
from asyncio import TimeoutError
from async_timeout import timeout as async_timeout
import hmac
import os
from contextlib import aclosing, asynccontextmanager, suppress
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from .config import load_settings
from .engine import Agent
from .events import envelope
from .storage import SessionBusy, encode
from .topology import snapshot


class ChatInput(BaseModel):
    """聊天输入；session_id 由调用方提供，禁止额外字段。"""
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=100000)
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


class PromptInput(BaseModel):
    """用户或会话提示词输入。"""
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(default="", max_length=2000)


class SearchInput(BaseModel):
    """关键词与字符窗口参数。"""
    terms: list[str] = Field(min_length=1, max_length=20)
    offset: int = Field(0, ge=0)
    limit: int = Field(6, ge=1, le=20)
    radius: int = Field(180, ge=0, le=1000)


def chat_response(agent, state, running):
    """用有界队列连接智能体与 SSE，并处理心跳和取消。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)  # 限制未发送事件数量

    async def produce():
        """将智能体事件连同序号推入队列，慢客户端会限制生产速度。"""
        cancelled = False
        try:
            seq = 0
            async with aclosing(agent.stream(state)) as stream:
                async for event in stream:
                    # 引擎为每个输出事件递增编号；这里按输出顺序重新计数即与其一致。
                    seq += 1
                    await queue.put({"seq": seq, "payload": event})
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # 取消时不等待满队列腾出位置。
            if not cancelled:
                await queue.put(None)

    task = asyncio.create_task(produce())
    running[state.run_id] = task

    async def cleanup():
        """断连后取消本轮任务，并释放运行名额。"""
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        running.pop(state.run_id, None)
        agent.store.finish_run(state.run_id, "cancelled")

    async def stream_sse():
        """将协议事件编码为 SSE，空闲时发送 server.heartbeat 事件。"""
        delivered_seq = 0
        yield "data: " + encode(envelope("server.connected", {})) + "\n\n"
        try:
            while True:
                if task.done() and queue.empty():
                    # 引擎已保存取消事件；补发给仍连接的客户端。
                    for record in agent.store.events(state.run_id, state.user_id, delivered_seq, 1000):
                        delivered_seq = record["seq"]
                        yield f"id: {record['seq']}\ndata: {encode(record['payload'])}\n\n"
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), agent.settings.agent.heartbeat_seconds)
                except TimeoutError:
                    if task.done():
                        continue
                    yield "data: " + encode(envelope("server.heartbeat", {})) + "\n\n"
                    continue
                if item is None:
                    break
                delivered_seq = item["seq"]
                yield f"id: {item['seq']}\ndata: {encode(item['payload'])}\n\n"
        finally:
            await cleanup()

    return StreamingResponse(stream_sse(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "X-Session-ID": state.session_id, "X-Run-ID": state.run_id,
    }, background=BackgroundTask(cleanup))


def create_app(settings=None, model=None) -> FastAPI:
    """创建业务接口及共享智能体，默认不启用自动接口文档。"""
    settings = settings or load_settings()
    agent = Agent(settings, model=model)
    running: dict[str, asyncio.Task] = {}  # run_id 到后台任务的映射

    @asynccontextmanager
    async def lifespan(app):
        """启动时恢复状态，退出时取消仍在运行的请求。"""
        agent.store.recover()
        yield
        tasks = list(running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.agent = agent
    app.state.running = running

    def user(request: Request):
        """校验可选令牌并获取上游提供的用户标识。"""
        token = os.getenv(settings.server.api_token_env, "")
        if token and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "需要有效的 Bearer Token")
        identity = request.headers.get("x-user-id", "local")
        if not identity.strip() or len(identity) > 128:
            raise HTTPException(400, "X-User-ID 需要 1..128 个字符")
        return identity

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SessionBusy)
    async def busy(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": settings.model.model, "mcp_servers": list(settings.mcp.servers),
                "skills": len(agent.skills.catalog), "skill_errors": agent.skills.errors}

    @app.post("/v1/session")
    async def create_session(user_id: str = Depends(user)):
        """生成 UUID 会话 ID 并预创建会话；调用方再把它作为 /v1/chat/stream 的 session_id。"""
        session_id = str(uuid4())
        session = agent.store.ensure_session(session_id, user_id)
        return {"session_id": session["id"]}

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, user_id: str = Depends(user)):
        session = agent.store.session(session_id, user_id)
        session.pop("context")
        return session

    @app.get("/v1/sessions/{session_id}/messages")
    async def messages(session_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), user_id: str = Depends(user)):
        return {"messages": agent.store.history(session_id, user_id, after, limit)}

    @app.put("/v1/prompts/user")
    async def user_prompt(body: PromptInput, user_id: str = Depends(user)):
        agent.store.set_user_prompt(user_id, body.prompt)
        return {"updated": True, "effective": "next_run"}

    @app.put("/v1/sessions/{session_id}/prompt")
    async def session_prompt(session_id: str, body: PromptInput, user_id: str = Depends(user)):
        agent.store.set_session_prompt(session_id, user_id, body.prompt)
        return {"updated": True, "effective": "next_run"}

    @app.get("/v1/skills")
    async def skills(query: str = "", offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=100), user_id: str = Depends(user)):
        return agent.skills.list(query, offset, limit)

    @app.get("/v1/runs/{run_id}")
    async def run_status(run_id: str, user_id: str = Depends(user)):
        return agent.store.run(run_id, user_id)

    @app.get("/v1/runs/{run_id}/events")
    async def events(run_id: str, after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000), user_id: str = Depends(user)):
        return {"events": agent.store.events(run_id, user_id, after_seq, limit)}

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str, user_id: str = Depends(user)):
        info = agent.store.run(run_id, user_id)
        if run_id in running:
            running[run_id].cancel()
            return {"status": "cancellation_requested"}
        return {"status": info["status"]}

    @app.get("/v1/sessions/{session_id}/artifacts/{artifact_id}")
    async def read_artifact(session_id: str, artifact_id: str, offset: int = Query(0, ge=0),
                            limit: int = Query(2000, ge=1, le=16000), user_id: str = Depends(user)):
        agent.store.session(session_id, user_id)
        return await asyncio.to_thread(agent.artifacts.read, session_id, artifact_id, offset, limit)

    @app.get("/v1/sessions/{session_id}/artifacts/{artifact_id}/raw")
    async def raw_artifact(session_id: str, artifact_id: str, user_id: str = Depends(user)):
        agent.store.session(session_id, user_id)
        path = agent.artifacts.raw_path(session_id, artifact_id)
        return FileResponse(path, media_type="application/json", filename=f"{artifact_id}.json")

    @app.post("/v1/sessions/{session_id}/artifacts/{artifact_id}/search")
    async def search_artifact(session_id: str, artifact_id: str, body: SearchInput, user_id: str = Depends(user)):
        agent.store.session(session_id, user_id)
        return await asyncio.to_thread(agent.artifacts.search, session_id, artifact_id, **body.model_dump())

    @app.post("/v1/chat/stream")
    async def chat(body: ChatInput, user_id: str = Depends(user)):
        """根据调用方会话 ID 启动本轮流式处理。"""
        state = agent.reserve(user_id, body.message, body.session_id)
        return chat_response(agent, state, running)

    @app.get("/v1/topology")
    async def topology(fabric_id: str = "", user_id: str = Depends(user)):
        """页面背景查询与诊断共用 MCP 配置和允许名单。"""
        try:
            async with async_timeout(settings.agent.tool_timeout_seconds):
                return await snapshot(agent.tools, settings.topology, fabric_id)
        except (ValueError, TimeoutError) as exc:
            raise HTTPException(502, f"拓扑查询失败：{exc}") from exc


    return app
