"""将引擎内部事件转换为 opencode 风格的对外 SSE 协议。

对外每条事件形如 {"id":"evt_...","type":"...","properties":{...}}。
会话与消息生命周期（session/message/part）与 opencode 事件模板对齐；
DCN 特有诊断事件（压缩、重试、限制等）沿用原类型名，套用同一信封。
"""
from __future__ import annotations

import time
from uuid import uuid4

from .budget import tokens
from .storage import encode


def now_ms() -> int:
    """模板中的 time 字段使用毫秒时间戳。"""
    return int(time.time() * 1000)


def unique_id(prefix: str) -> str:
    """生成 evt_/prt_/msg_/call_ 等前缀的唯一 ID。"""
    return f"{prefix}{now_ms():x}{uuid4().hex[:16]}"


def token_usage(input_value: int = 0, output_value: int = 0) -> dict:
    """模板 tokens 字段结构；本项目使用 UTF-8 字节保守估算。"""
    return {"total": input_value + output_value, "input": input_value, "output": output_value,
            "reasoning": 0, "cache": {"write": 0, "read": 0}}


def envelope(event_type: str, properties: dict) -> dict:
    """组装统一事件信封。"""
    return {"id": unique_id("evt_"), "type": event_type, "properties": properties}


class Protocol:
    """一次运行的对外事件转换器，维护消息和分段的编号线索。"""

    def __init__(self, session_id: str, run_id: str, goal: str, model_id: str, provider_id: str, created_ms: int):
        self.session_id = f"ses_{session_id}"
        self.run_id = run_id
        self.goal = goal
        self.model = {"id": model_id, "providerID": provider_id, "variant": "default"}
        self.created_ms = created_ms
        self.usage = {"input": 0, "output": 0}  # 本轮累计估算
        self.user_message = ""                  # 本轮用户消息 ID
        self.message = ""                       # 当前助手消息 ID
        self.message_created = 0
        self.step_open = False                  # 当前消息是否有未收尾的步骤
        self.step_input = 0                     # 当前步骤输入估算
        self.step_output = 0                    # 当前步骤输出估算
        self.text_part = ""                     # 最终回答文本分段 ID
        self.text_start = 0
        self.calls: dict[str, dict] = {}        # call_id 到工具分段线索的映射

    # ---- 组装基础事件 ----

    def _part(self, part: dict) -> dict:
        """补充分段归属字段并包装为 message.part.updated。"""
        part.setdefault("messageID", self.message)
        part.setdefault("sessionID", self.session_id)
        part.setdefault("id", unique_id("prt_"))
        return envelope("message.part.updated", {"sessionID": self.session_id, "part": part, "time": now_ms()})

    def _message(self, info: dict) -> dict:
        """包装为 message.updated。"""
        info.setdefault("sessionID", self.session_id)
        return envelope("message.updated", {"sessionID": self.session_id, "info": info})

    def _assistant_info(self, message_id: str, extra: dict | None = None) -> dict:
        """助手消息的公共字段；extra 补充 completed/finish 等收尾字段。"""
        info = {"id": message_id, "parentID": self.user_message, "role": "assistant", "mode": "build",
                "agent": "build", "cost": 0, "tokens": token_usage(self.step_input, self.step_output),
                "modelID": self.model["id"], "providerID": self.model["providerID"],
                "time": {"created": self.message_created}}
        if extra:
            info.update(extra)
        return info

    def _session_info(self) -> dict:
        """会话摘要信息；标题取本轮问题前 80 个字符。"""
        return {"id": self.session_id, "slug": self.session_id[4:], "projectID": "dcn-agent",
                "directory": "", "path": "", "title": self.goal[:80] or "New session",
                "agent": "build", "model": self.model, "version": "1.0", "cost": 0,
                "tokens": {**token_usage(self.usage["input"], self.usage["output"]),
                           "input": self.usage["input"], "output": self.usage["output"]},
                "time": {"created": self.created_ms, "updated": now_ms()}}

    def _close_step(self, reason: str) -> list[dict]:
        """发出 step-finish 并结束当前助手消息。"""
        self.step_open = False
        self.usage["output"] += self.step_output
        step_tokens = token_usage(self.step_input, self.step_output)
        return [self._part({"type": "step-finish", "reason": reason, "tokens": step_tokens, "cost": 0}),
                self._message(self._assistant_info(self.message, {
                    "tokens": step_tokens, "finish": reason,
                    "time": {"created": self.message_created, "completed": now_ms()}}))]

    def _reasoning_part(self, kind: str, text: str) -> list[dict]:
        """决策依据类的文字作为 reasoning 分段输出。"""
        self.step_output += tokens(text)
        moment = now_ms()
        return [self._part({"type": "reasoning", "text": text, "metadata": {"kind": kind},
                            "time": {"start": moment, "end": moment}})]

    # ---- 内部事件的映射实现 ----

    def _run_started(self, data: dict) -> list[dict]:
        """本轮开始：会话信息、用户消息与文本分段、进入忙碌状态。"""
        self.user_message = f"msg_{self.run_id}u"
        return [envelope("session.updated", {"sessionID": self.session_id, "info": self._session_info()}),
                self._message({"id": self.user_message, "role": "user", "time": {"created": now_ms()},
                               "agent": "build", "model": {"providerID": self.model["providerID"],
                                                           "modelID": self.model["id"]}}),
                self._part({"type": "text", "text": self.goal, "messageID": self.user_message}),
                envelope("session.status", {"sessionID": self.session_id, "status": {"type": "busy"}})]

    def _model_started(self, data: dict) -> list[dict]:
        """每次模型调用开启一条助手消息和 step-start 分段；决策步骤按序号复用。"""
        events: list[dict] = []
        if self.step_open:  # 上一步直接给出 final 决策时，为上条消息补收尾。
            events += self._close_step("stop")
        message_id = f"msg_{self.run_id}f" if data.get("phase") == "answer" else f"msg_{self.run_id}s{data.get('step', 1)}"
        if self.message != message_id:
            self.message = message_id
            self.message_created = now_ms()
            events.append(self._message(self._assistant_info(message_id)))
        self.step_input = data.get("input_tokens", 0)
        self.step_output = 0
        self.usage["input"] += self.step_input
        self.step_open = True
        events.append(self._part({"type": "step-start", "metadata": {"phase": data.get("phase"),
                                                                     "step": data.get("step", 0),
                                                                     "attempt": data.get("attempt", 1)}}))
        return events

    def _tool_call(self, data: dict) -> list[dict]:
        """工具调用先发 pending，再带参数发 running。"""
        arguments = data.get("arguments") or {}
        part_id = unique_id("prt_")
        self.calls[data["call_id"]] = {"part_id": part_id, "tool": data["name"], "input": arguments,
                                       "title": arguments.get("name", data["name"]) if data["name"] == "mcp.call" else data["name"],
                                       "start": 0}
        self.step_output += tokens(encode(arguments))
        part = {"id": part_id, "type": "tool", "tool": data["name"], "callID": "call_" + data["call_id"],
                "state": {"status": "pending", "input": {}, "raw": ""}}
        events = [self._part(part)]
        start = now_ms()
        self.calls[data["call_id"]]["start"] = start
        part = {**part, "state": {"status": "running", "input": arguments, "title": self.calls[data["call_id"]]["title"],
                                  "metadata": {"step": data.get("step")}, "time": {"start": start}}}
        events.append(self._part(part))
        return events

    def _tool_result(self, data: dict) -> list[dict]:
        """工具返回 completed/error，随后结束当前步骤。"""
        entry = self.calls.get(data["call_id"], {"part_id": unique_id("prt_"), "tool": data.get("name", ""),
                                                 "input": {}, "title": "", "start": now_ms()})
        end = now_ms()
        ok = data.get("ok", True)
        state = {"status": "completed" if ok else "error", "input": entry["input"], "output": encode(data),
                 "metadata": {"truncated": data.get("truncated", False), "ok": ok, "step": data.get("step"),
                              "artifact_id": data.get("artifact_id"), "chars": data.get("chars")},
                 "title": entry["title"], "time": {"start": entry["start"], "end": end}}
        return [self._part({"id": entry["part_id"], "type": "tool", "tool": entry["tool"],
                            "callID": "call_" + data["call_id"], "state": state}),
                *self._close_step("tool-calls")]

    def _answer_delta(self, data: dict) -> list[dict]:
        """首个增量前开启文本分段，其后逐条转发 message.part.delta。"""
        events: list[dict] = []
        if not self.text_part:
            self.text_part = unique_id("prt_")
            self.text_start = now_ms()
            events.append(self._part({"id": self.text_part, "type": "text", "text": "",
                                      "time": {"start": self.text_start}}))
        self.step_output += tokens(data["text"])
        events.append(envelope("message.part.delta", {"sessionID": self.session_id, "messageID": self.message,
                                                      "partID": self.text_part, "field": "text",
                                                      "delta": data["text"]}))
        return events

    def _answer_completed(self, data: dict) -> list[dict]:
        """补全文本分段并结束当前步骤。"""
        events = [self._part({"id": self.text_part or unique_id("prt_"), "type": "text", "text": data.get("text", ""),
                              "time": {"start": self.text_start or now_ms(), "end": now_ms()}})]
        if self.step_open:
            events += self._close_step("stop")
        return events

    def _run_completed(self, data: dict) -> list[dict]:
        """本轮结束：回到空闲并携带最终状态。"""
        return [envelope("session.status", {"sessionID": self.session_id, "status": {"type": "idle"}}),
                envelope("session.idle", {"sessionID": self.session_id, "status": data.get("status"),
                                          "steps": data.get("steps"), "runID": self.run_id})]

    def _run_cancelled(self, data: dict) -> list[dict]:
        """取消与完成一样以空闲收尾，状态标记 cancelled。"""
        return [envelope("session.status", {"sessionID": self.session_id, "status": {"type": "idle"}}),
                envelope("session.idle", {"sessionID": self.session_id, "status": "cancelled",
                                          "runID": self.run_id, "reason": data.get("reason")})]

    # ---- 事件入口 ----

    def translate(self, event_type: str, data: dict) -> list[dict]:
        """内部事件到协议事件的映射；一个内部事件可展开为多个协议事件。"""
        handlers = {"run.started": self._run_started, "model.started": self._model_started,
                    "tool.call": self._tool_call, "tool.result": self._tool_result,
                    "answer.delta": self._answer_delta, "answer.completed": self._answer_completed,
                    "run.completed": self._run_completed, "run.cancelled": self._run_cancelled,
                    "plan": lambda d: self._reasoning_part("plan", "\n".join(f"{i}. {s}" for i, s in enumerate(d.get("steps", []), 1))),
                    "reasoning": lambda d: self._reasoning_part("summary", d.get("text", "")),
                    "reflection": lambda d: self._reasoning_part("reflection", d.get("text", ""))}
        handler = handlers.get(event_type)
        if handler is None:
            # 上下文压缩、重试、限制、澄清、错误等 DCN 特有事件沿用原类型名。
            return [envelope(event_type, {"sessionID": self.session_id, **data})]
        return handler(data)
