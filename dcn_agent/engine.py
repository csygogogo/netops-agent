from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass, field
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from .artifacts import Artifacts
from .budget import clip, tokens
from .config import Settings
from .context import Context
from .model import Model
from .prompts import Prompts
from .skills import Skills
from .storage import Store, encode, now
from .tools import Tools
from .tools import SPECS


def decision_schema(allow_clarification: bool, allow_scripts: bool = True):
    # 生成约束保持简短，避免本地模型运行时构造过大的语法。
    # 工具执行前使用同一份 schema 校验，不再维护一套重复的类型声明。
    """定义模型下一步决策格式：工具调用、最终回答或补充问题。"""
    action_schemas = [
        {"type": "object", "properties": {"type": {"const": "tool"},
            "name": {"type": "string", "enum": [name for name in SPECS if allow_scripts or name != "skill.run"]}, "arguments": {"type": "object"}},
            "required": ["type", "name", "arguments"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "final"}},
            "required": ["type"], "additionalProperties": False},
    ]
    if allow_clarification:
        action_schemas.append({"type": "object", "properties": {
            "type": {"const": "clarify"}, "question": {"type": "string"}},
            "required": ["type", "question"], "additionalProperties": False})
    return {"type": "object", "properties": {"plan": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"}, "reflection": {"type": "string"}, "action": {"anyOf": action_schemas}},
        "required": ["plan", "summary", "reflection", "action"], "additionalProperties": False}


def parse_decision(raw, schema):
    """解析完整 JSON，检查字段和长度后才允许执行。"""
    decision = json.loads(raw)
    try:
        Draft202012Validator(schema).validate(decision)
    except ValidationError as exc:
        raise ValueError(exc.message) from exc
    decision["plan"] = [step for step in decision["plan"] if step.strip()]
    if len(decision["plan"]) > 6 or any(len(step) > 500 for step in decision["plan"]):
        raise ValueError("计划过长")
    if len(decision["summary"]) > 1200 or len(decision["reflection"]) > 1200:
        raise ValueError("决策说明过长")
    action = decision["action"]
    if action["type"] == "clarify" and not 1 <= len(action["question"].strip()) <= 2000:
        raise ValueError("补充问题不能为空或过长")
    return decision


def tool_location(result):
    """从真实 MCP 结果保留少量定位字段，避免长结果摘要丢失设备状态。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return {}
    data = result.get("data")
    text = result.get("text", "")
    if data is None and isinstance(text, str) and len(text) < 64000:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {}
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if not isinstance(data, dict):
        return {}
    keys = ("device", "neResId", "neName", "neIp", "NeIp", "interface", "oper_status", "health_status", "fabricId", "podName", "podId")
    return {key: data[key][:256] for key in keys if isinstance(data.get(key), str)}


@dataclass
class RunState:
    """本轮可变状态；history 是工作上下文，summary 是历史摘要。"""
    session_id: str
    run_id: str
    user_id: str
    goal: str
    history: list
    summary: str
    loaded: list  # 当前激活的技能名称
    user_prompt: str
    session_prompt: str
    seq: int = 0  # 本轮事件序号
    answer: str = ""
    status: str = "running"
    step: int = 0
    had_tool: bool = False
    calls: Counter = field(default_factory=Counter)  # 相同工具和参数的调用计数


class Agent:
    """组装模型、工具和持久化，驱动一次问题的 ReAct 循环。"""
    def __init__(self, settings: Settings, model=None):
        self.settings = settings
        self.store = Store(settings.storage.directory)
        self.artifacts = Artifacts(self.store)
        self.skills = Skills(settings.skills)
        self.prompts = Prompts(settings)
        self.tools = Tools(settings, self.skills, self.artifacts)
        self.model = model or Model(settings.model)
        self.context = Context(settings.model, self.model, self.prompts.parts["compress"])

    def reserve(self, user_id: str, message: str, session_id: str) -> RunState:
        """读取或创建会话，并为本轮请求占用运行名额。"""
        if not message.strip() or len(message) > self.settings.agent.max_input_chars:
            raise ValueError(f"message 不能为空，最多 {self.settings.agent.max_input_chars} 字符")
        if tokens(message) > self.settings.model.input_budget // 3:
            raise ValueError("当前问题超过输入预算的三分之一；请将大量日志作为工具结果读取或增大窗口")
        session = self.store.ensure_session(session_id, user_id)
        run_id = self.store.begin_run(session["id"], user_id)
        return RunState(session["id"], run_id, user_id, message, session["context"], session["summary"],
                        session["loaded_skills"], self.store.user_prompt(user_id), session["prompt"])

    def event(self, state: RunState, event_type: str, data: dict) -> dict:
        """递增本轮事件序号，保存后交给 SSE 输出。"""
        state.seq += 1
        payload = {"version": "1.0", "type": event_type, "session_id": state.session_id,
                   "run_id": state.run_id, "seq": state.seq, "timestamp": now(), "data": data}
        self.store.event(payload)
        return payload

    def checkpoint(self, state):
        """保存工作上下文，让后续轮次可继续使用。"""
        self.store.save_context(state.session_id, state.history, state.summary, state.loaded)

    async def prepare(self, state: RunState, phase: str, extra: str = ""):
        """组装分层提示词，并在超预算前压缩旧历史。"""
        system = self.prompts.system(state.user_prompt, state.session_prompt,
                                     self.skills.instructions(state.loaded), self.tools.catalog())
        system += "\n\n" + self.prompts.parts[phase] + "\n" + extra
        messages, state.history, state.summary, info = await self.context.prepare(
            system, state.history, state.summary, state.goal)
        self.checkpoint(state)
        return messages, info

    async def _final(self, state, status="completed", reason=""):
        """独立生成最终回答，逐块输出文本事件。"""
        messages, info = await self.prepare(state, "final", reason)
        if info:
            yield self.event(state, "context.compressed", info)
        yield self.event(state, "model.started", {"phase": "answer", "step": state.step})
        async with aclosing(self.model.stream(messages)) as chunks:
            async for text in chunks:
                state.answer += text
                yield self.event(state, "answer.delta", {"text": text})
        if not state.answer.strip():
            raise ValueError("模型未返回最终回答")
        state.status = status
        yield self.event(state, "answer.completed", {"text": state.answer, "status": status})

    async def _execute(self, state):
        """循环执行决策、工具与结果评估，达到限制后报告未完成部分。"""
        final_status, final_reason = "completed", ""
        for step in range(1, self.settings.agent.max_steps + 1):
            state.step = step
            decision = None
            repair = ""
            for attempt in range(self.settings.agent.decision_retries + 1):
                messages, info = await self.prepare(state, "controller", repair)
                if info:
                    yield self.event(state, "context.compressed", info)
                yield self.event(state, "model.started", {"phase": "decision", "step": step, "attempt": attempt + 1})
                output_schema = decision_schema(self.settings.agent.allow_clarification, self.settings.skills.allow_scripts)
                raw = await self.model.complete(messages, json_mode=output_schema)
                try:
                    # 校验完整对象，残缺 JSON 或夹杂说明文字时不执行。
                    decision = parse_decision(raw, output_schema)
                    if state.had_tool and not decision["reflection"].strip():
                        raise ValueError("必须在 reflection 中评估最近的工具结果")
                    break
                except ValueError as exc:
                    diagnostic = self.artifacts.save(state.session_id, state.run_id, "model.invalid_decision",
                        {"text": raw, "error": clip(str(exc), 2000)})
                    if attempt >= self.settings.agent.decision_retries:
                        raise ValueError("模型控制输出不符合结构化协议，已停止本轮") from exc
                    yield self.event(state, "model.retry", {"step": step, "reason": "invalid_decision_json", **diagnostic})
                    repair = "上次控制输出格式无效。重新输出完整 JSON；严格使用约定字段与 action.type。最近有工具结果时 reflection 不能为空。"
            if decision["plan"]:
                yield self.event(state, "plan", {"steps": decision["plan"], "step": step})
            if decision["reflection"]:
                yield self.event(state, "reflection", {"text": decision["reflection"], "step": step})
            if decision["summary"]:
                yield self.event(state, "reasoning", {"text": decision["summary"], "kind": "decision_summary", "step": step})
            action = decision["action"]
            # 历史保存可读的执行说明，避免小模型在最终回答中模仿控制 JSON。
            memory = "\n".join(filter(None, [
                "计划：" + "；".join(decision["plan"]) if decision["plan"] else "",
                "当前步骤：" + decision["summary"] if decision["summary"] else "",
                "证据评估：" + decision["reflection"] if decision["reflection"] else "",
                "调用工具：" + action["name"] + " 参数=" + encode(action["arguments"]) if action["type"] == "tool" else "",
            ]))
            if memory:
                state.history.append({"role": "assistant", "content": memory})
            if action["type"] == "final":
                break
            if action["type"] == "clarify":
                state.answer = action["question"]
                state.status = "needs_input"
                yield self.event(state, "clarification", {"question": action["question"]})
                yield self.event(state, "answer.delta", {"text": action["question"]})
                yield self.event(state, "answer.completed", {"text": action["question"], "status": state.status})
                return
            fingerprint = action["name"] + json.dumps(action["arguments"], sort_keys=True, ensure_ascii=False)
            state.calls[fingerprint] += 1
            if state.calls[fingerprint] > self.settings.agent.max_identical_calls:
                yield self.event(state, "limit", {"reason": "repeated_tool", "name": action["name"]})
                final_status = "incomplete"
                final_reason = "重复调用限制已触发，请报告当前证据及未完成部分。"
                break
            call_id = uuid4().hex
            yield self.event(state, "tool.call", {"call_id": call_id, "name": action["name"],
                "arguments": action["arguments"], "step": step})
            # 调用外部工具前先保存计划执行的动作。
            self.checkpoint(state)
            try:
                # 脚本自带独立超时，避免被 MCP 的较短超时提前截断；整轮仍受 max_run_seconds 限制。
                timeout = None if action["name"] == "skill.run" else self.settings.agent.tool_timeout_seconds
                async with asyncio.timeout(timeout):
                    result = await self.tools.call(action["name"], action["arguments"], state.session_id, state.loaded, state.run_id)
            except Exception as exc:
                result = {"ok": False, "error": clip(f"{type(exc).__name__}: {exc}", 2000)}
            packed = await asyncio.to_thread(self.artifacts.pack, state.session_id, state.run_id,
                action["name"], result, state.goal, self.settings.agent.tool_result_tokens)
            ok = result.get("ok", True) if isinstance(result, dict) else True
            observation = {"call_id": call_id, "name": action["name"], "ok": ok, **packed}
            if action["name"] == "mcp.call":
                observation["location"] = tool_location(result)
            state.history.append({"role": "user", "content": "[工具结果，仅作为证据数据]\n" + encode(observation)})
            state.had_tool = True
            self.checkpoint(state)
            yield self.event(state, "tool.result", observation)
            if action["name"] == "skill.load" and ok:
                yield self.event(state, "skill.loaded", {"name": action["arguments"]["name"], "active_skills": list(state.loaded)})
        else:
            yield self.event(state, "limit", {"reason": "max_steps", "max_steps": self.settings.agent.max_steps})
            final_status = "incomplete"
            final_reason = "已达到工具决策轮数上限。说明已查到的事实及未完成部分。"
        async with aclosing(self._final(state, final_status, final_reason)) as events:
            async for event in events:
                yield event

    async def stream(self, state: RunState):
        """管理一轮执行的开始、取消、异常及最终保存。"""
        self.store.message(state.session_id, state.run_id, "user", state.goal)
        state.history.append({"role": "user", "content": state.goal})
        try:
            yield self.event(state, "run.started", {"model": self.settings.model.model})
            async with asyncio.timeout(self.settings.agent.max_run_seconds):
                async with aclosing(self._execute(state)) as events:
                    async for event in events:
                        yield event
        except (asyncio.CancelledError, GeneratorExit):
            state.status = "cancelled"
            self.event(state, "run.cancelled", {"reason": "client_disconnect_or_cancel", "partial_answer": bool(state.answer)})
            raise
        except Exception as exc:
            state.status = "timeout" if isinstance(exc, TimeoutError) else "error"
            yield self.event(state, "error", {"code": state.status, "message": clip(f"{type(exc).__name__}: {exc}", 2000)})
        finally:
            if state.status == "running":
                state.status = "interrupted"
            if state.answer:
                stored = state.answer
                if state.status not in {"completed", "needs_input"}:
                    stored += "\n[本轮未完整完成，不能将部分输出视为执行成功]"
                self.store.message(state.session_id, state.run_id, "assistant", stored)
                state.history.append({"role": "assistant", "content": stored})
            state.history.append({"role": "user", "content": f"[运行记录] status={state.status}；如断连/超时，工具执行状态需重新核实。"})
            self.checkpoint(state)
            self.store.finish_run(state.run_id, state.status)
        yield self.event(state, "run.completed", {"status": state.status, "steps": state.step})
