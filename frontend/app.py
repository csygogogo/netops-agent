#!/usr/bin/env python3
"""前端静态服务与后端流式代理；隐患界面保留原演示。"""

from __future__ import annotations

import json
import os
import sys

import httpx
import mimetypes
import queue
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CONFIG_PATH = ROOT / "config.json"
# 直接运行 frontend/app.py 时，仍从项目根目录读取统一配置。
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
from dcn_agent.config import load_settings
SETTINGS = load_settings()
MOCK_DELAY_SECONDS = 0.1

DEFAULT_CONFIG = {
    "features": {
        "perception": True,
        "deep_diagnosis": True,
    }
}


def load_config() -> dict:
    try:
        configured = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        configured = {}

    configured_features = configured.get("features", {}) if isinstance(configured, dict) else {}
    features = {}
    for name, default in DEFAULT_CONFIG["features"].items():
        value = configured_features.get(name, default) if isinstance(configured_features, dict) else default
        features[name] = value if isinstance(value, bool) else default
    return {"features": features}


def feature_enabled(name: str) -> bool:
    return bool(load_config()["features"].get(name, True))


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


RISKS = [
    {
        "id": "optic-leaf-07",
        "title": "Leaf-07 上联链路光模块衰减异常",
        "scope": "Leaf-07 / Spine-02",
        "level": "高风险",
        "status": "可执行",
        "description": "接收光功率持续低于健康基线，伴随 CRC 错包缓慢上升。",
        "topology": {
            "devices": [
                {"name": "Spine-01", "device_type": "spine", "has_problem": False},
                {"name": "Spine-02", "device_type": "spine", "has_problem": False},
                {"name": "Leaf-05", "device_type": "leaf", "has_problem": False},
                {"name": "Leaf-06", "device_type": "leaf", "has_problem": False},
                {"name": "Leaf-07", "device_type": "leaf", "has_problem": True},
                {"name": "Leaf-08", "device_type": "leaf", "has_problem": False},
                {"name": "业务区 A", "device_type": "server_group", "has_problem": False},
                {"name": "业务区 B", "device_type": "server_group", "has_problem": False},
            ],
            "links": [
                {"source": "Spine-01", "target": "Leaf-05", "has_problem": False},
                {"source": "Spine-01", "target": "Leaf-06", "has_problem": False},
                {"source": "Spine-01", "target": "Leaf-07", "has_problem": False},
                {"source": "Spine-01", "target": "Leaf-08", "has_problem": False},
                {"source": "Spine-02", "target": "Leaf-05", "has_problem": False},
                {"source": "Spine-02", "target": "Leaf-06", "has_problem": False},
                {"source": "Spine-02", "target": "Leaf-07", "has_problem": True},
                {"source": "Spine-02", "target": "Leaf-08", "has_problem": False},
                {"source": "Leaf-06", "target": "业务区 A", "has_problem": False},
                {"source": "Leaf-07", "target": "业务区 B", "has_problem": False},
            ],
        },
    },
    {
        "id": "bgp-spine-flap",
        "title": "Spine-02 与 Spine-03 间 BGP 邻居频繁 flap",
        "scope": "核心层 / 2 台设备",
        "level": "高风险",
        "status": "可执行",
        "description": "BGP 邻居在短时间内多次重建，可能引发业务路径抖动。",
        "topology": {
            "devices": [
                {"name": "Spine-01", "device_type": "spine", "has_problem": False},
                {"name": "Spine-02", "device_type": "spine", "has_problem": True},
                {"name": "Spine-03", "device_type": "spine", "has_problem": True},
                {"name": "Leaf-01", "device_type": "leaf", "has_problem": False},
                {"name": "Leaf-02", "device_type": "leaf", "has_problem": False},
                {"name": "Leaf-03", "device_type": "leaf", "has_problem": False},
            ],
            "links": [
                {"source": "Spine-02", "target": "Spine-03", "has_problem": True},
                {"source": "Spine-01", "target": "Leaf-01", "has_problem": False},
                {"source": "Spine-02", "target": "Leaf-02", "has_problem": False},
                {"source": "Spine-03", "target": "Leaf-03", "has_problem": False},
            ],
        },
    },
    {
        "id": "ecmp-imbalance",
        "title": "Leaf-12 到 Spine-01 的 ECMP 流量分布不均",
        "scope": "Leaf-12 / 4 条路径",
        "level": "中风险",
        "status": "可执行",
        "description": "单条 ECMP 成员链路承载流量显著高于其他成员。",
        "topology": {
            "devices": [
                {"name": "Spine-01", "device_type": "spine", "has_problem": False},
                {"name": "Spine-02", "device_type": "spine", "has_problem": False},
                {"name": "Leaf-12", "device_type": "leaf", "has_problem": True},
                {"name": "业务集群 C", "device_type": "server_group", "has_problem": False},
            ],
            "links": [
                {"source": "Spine-01", "target": "Leaf-12", "has_problem": True},
                {"source": "Spine-02", "target": "Leaf-12", "has_problem": False},
                {"source": "Leaf-12", "target": "业务集群 C", "has_problem": False},
            ],
        },
    },
    {
        "id": "buffer-pressure",
        "title": "Spine-01 egress buffer 压力持续偏高",
        "scope": "Spine-01 / 8 个端口",
        "level": "中风险",
        "status": "可执行",
        "description": "出方向缓存占用在流量高峰期长期超过告警水位。",
        "topology": {
            "devices": [
                {"name": "Spine-01", "device_type": "spine", "has_problem": True},
                {"name": "Leaf-21", "device_type": "leaf", "has_problem": False},
                {"name": "Leaf-22", "device_type": "leaf", "has_problem": False},
                {"name": "Leaf-23", "device_type": "leaf", "has_problem": False},
            ],
            "links": [
                {"source": "Spine-01", "target": "Leaf-21", "has_problem": False},
                {"source": "Spine-01", "target": "Leaf-22", "has_problem": True},
                {"source": "Spine-01", "target": "Leaf-23", "has_problem": False},
            ],
        },
    },
    {
        "id": "crc-leaf-03",
        "title": "Leaf-03 接口 CRC 错误率持续上升",
        "scope": "Leaf-03 / Ethernet1/49",
        "level": "低风险",
        "status": "可执行",
        "description": "CRC 增量缓慢上升，当前尚未造成明显业务影响。",
        "topology": {
            "devices": [
                {"name": "Spine-02", "device_type": "spine", "has_problem": False},
                {"name": "Leaf-03", "device_type": "leaf", "has_problem": True},
                {"name": "计算节点组", "device_type": "server_group", "has_problem": False},
            ],
            "links": [
                {"source": "Spine-02", "target": "Leaf-03", "has_problem": True},
                {"source": "Leaf-03", "target": "计算节点组", "has_problem": False},
            ],
        },
    },
]


class PerceptionHub:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._executions: dict[str, dict] = {}

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(client)

    def publish(self, payload: dict) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for client in subscribers:
            try:
                client.put_nowait(payload)
            except queue.Full:
                pass

    def list_executions(self) -> list[dict]:
        with self._lock:
            return deepcopy(list(reversed(self._executions.values())))

    def get_execution(self, execution_id: str) -> dict | None:
        with self._lock:
            execution = self._executions.get(execution_id)
            return deepcopy(execution) if execution else None

    def begin_diagnosis(self, execution_id: str, question: str) -> dict | None:
        with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                return None
            diagnosis = execution.get("diagnosis") or {
                "steps": [],
                "turns": [],
                "topology": None,
                "report_markdown": None,
            }
            if not diagnosis.get("turns") and diagnosis.get("steps"):
                diagnosis["turns"] = [{
                    "id": f"legacy-{uuid.uuid4().hex[:8]}",
                    "question": diagnosis.get("question", "历史诊断"),
                    "status": diagnosis.get("status", "completed"),
                    "steps": deepcopy(diagnosis["steps"]),
                    "updated_at": diagnosis.get("updated_at", now_text()),
                }]
            turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            diagnosis.setdefault("steps", [])
            diagnosis.setdefault("turns", []).append({
                "id": turn_id,
                "question": question,
                "status": "running",
                "steps": [],
                "updated_at": now_text(),
            })
            diagnosis["active_turn_id"] = turn_id
            diagnosis["status"] = "running"
            diagnosis["question"] = question
            diagnosis["updated_at"] = now_text()
            execution["diagnosis"] = diagnosis
            return deepcopy(execution)

    def append_diagnosis_step(self, execution_id: str, step: dict) -> None:
        with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                return
            diagnosis = execution.get("diagnosis")
            if not diagnosis:
                return
            diagnosis["steps"].append(deepcopy(step))
            diagnosis["updated_at"] = now_text()
            active_turn_id = diagnosis.get("active_turn_id")
            active_turn = next((turn for turn in reversed(diagnosis.get("turns", [])) if turn["id"] == active_turn_id), None)
            if active_turn:
                active_turn["steps"].append(deepcopy(step))
                active_turn["updated_at"] = diagnosis["updated_at"]
            topology = topology_from_step(step)
            if topology:
                diagnosis["topology"] = deepcopy(topology)
            if is_final_agent_event(step):
                report_markdown = str(agent_event_content(step))
                diagnosis["status"] = "completed"
                diagnosis["report_markdown"] = report_markdown
                if active_turn:
                    active_turn["status"] = "completed"
                    active_turn["report_markdown"] = report_markdown
                diagnosis.pop("active_turn_id", None)

    def seed_completed(self, risks: list[dict]) -> None:
        seed_events = [
            "任务已下发｜历史 Mock 执行记录",
            "拓扑已更新｜已定位涉及的设备与链路",
            "检测到隐患｜关键指标偏离健康基线",
            "风险确认｜影响范围已完成评估",
            "报告已生成｜根因、证据与处置建议已整理完成",
        ]
        with self._lock:
            for index, risk in enumerate(risks[:4], start=1):
                execution_id = f"history-{index:03d}-{risk['id']}"
                self._executions[execution_id] = {
                    "id": execution_id,
                    "template_id": risk["id"],
                    "title": risk["title"],
                    "description": risk["description"],
                    "scope": risk["scope"],
                    "level": risk["level"],
                    "status": "completed",
                    "events": list(seed_events),
                    "topology": deepcopy(risk["topology"]),
                    "report_markdown": build_report(risk),
                    "diagnosis": None,
                    "created_at": f"历史记录 {index}",
                    "updated_at": now_text(),
                }
                if index == 1:
                    steps = build_execution_diagnosis_steps(
                        self._executions[execution_id],
                        "基于这份隐患分析报告进一步诊断，确认根因并给出处置方案。",
                    )
                    self._executions[execution_id]["diagnosis"] = diagnosis_from_steps(steps)

    def dispatch(self, risk: dict) -> dict:
        execution_id = f"exec-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
        execution = {
            "id": execution_id,
            "template_id": risk["id"],
            "title": risk["title"],
            "description": risk["description"],
            "scope": risk["scope"],
            "level": risk["level"],
            "status": "running",
            "events": [],
            "topology": None,
            "report_markdown": None,
            "diagnosis": None,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": now_text(),
        }
        with self._lock:
            self._executions[execution_id] = execution
            self._active.add(execution_id)
        threading.Thread(target=self._run, args=(execution_id, risk), daemon=True).start()
        return deepcopy(execution)

    def _run(self, execution_id: str, risk: dict) -> None:
        events: list[str] = []
        sequence = [
            {"message": f"任务已下发｜{risk['title']}"},
            {"message": "任务校验完成｜Mock 场景参数与目标设备匹配"},
            {"message": "感知服务已加载场景数据并开始持续检查"},
            {
                "message": "拓扑已更新｜已定位本次感知涉及的设备与链路",
                "tag": "topology",
                "data": risk["topology"],
            },
            {"message": "遥测订阅建立｜接口指标、错误包与光功率数据开始回流"},
            {"message": "健康基线完成｜已加载过去 24 小时同周期正常区间"},
            {"message": "检测到隐患｜关键指标连续三个周期偏离健康基线"},
            {"message": "异常交叉验证｜设备状态、链路状态与指标变化方向一致"},
            {"message": "业务路径评估｜正在计算异常链路关联的业务与冗余路径"},
            {"message": f"风险确认｜影响范围：{risk['scope']}"},
            {"message": "根因推断完成｜已排除流量突增与控制面抖动假设"},
            {"message": "处置建议生成｜已结合风险等级生成操作优先级"},
            {"message": "报告已生成｜根因、证据与处置建议已整理完成"},
        ]
        for index, step in enumerate(sequence):
            time.sleep(MOCK_DELAY_SECONDS)
            events.append(step["message"])
            payload = {
                "execution_id": execution_id,
                "risk_id": risk["id"],
                "risk_title": risk["title"],
                "events": list(events),
                "current_index": index,
                "state": "completed" if index == len(sequence) - 1 else "running",
                "timestamp": now_text(),
            }
            if step.get("tag") == "topology":
                payload["tag"] = "topology"
                payload["data"] = step["data"]
            if index == len(sequence) - 1:
                payload["report_markdown"] = build_report(risk)
            with self._lock:
                execution = self._executions[execution_id]
                execution["events"] = list(events)
                execution["status"] = payload["state"]
                execution["updated_at"] = payload["timestamp"]
                if step.get("tag") == "topology":
                    execution["topology"] = deepcopy(step["data"])
                if payload.get("report_markdown"):
                    execution["report_markdown"] = payload["report_markdown"]
            self.publish(payload)
        with self._lock:
            self._active.discard(execution_id)


def build_report(risk: dict) -> str:
    return f"""## 隐患结论

**{risk['title']}** 已被感知服务确认，风险等级为 **{risk['level']}**。

### 关键证据

- 关键指标连续 3 个采样周期偏离健康基线
- 异常范围与任务描述中的设备、链路一致
- 影响范围：`{risk['scope']}`

### 处置建议

1. 现场复核异常设备或链路的物理状态
2. 对比相邻端口指标，排除对端与环境因素
3. 低峰期执行替换或切流，并持续观察 15 分钟
"""


def agent_event_content(event: dict):
    """Read the new content field while retaining old saved-step support."""
    return event.get("content", event.get("raw", ""))


def is_final_agent_event(event: dict) -> bool:
    return str(event.get("tag", "")).strip() == "最终结果" or bool(event.get("final"))


def decode_json_content(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def topology_from_value(value, depth: int = 0) -> dict | None:
    if depth > 3:
        return None
    value = decode_json_content(value)
    if not isinstance(value, dict):
        return None

    tag = str(value.get("tag", value.get("type", value.get("kind", "")))).lower()
    if tag == "topology":
        topology = value.get("data") or value.get("topology") or value.get("raw")
        topology = decode_json_content(topology)
        if isinstance(topology, dict) and isinstance(topology.get("devices"), list) and isinstance(topology.get("links"), list):
            return topology

    for key in ("content", "result", "data", "raw", "topology"):
        nested = value.get(key)
        if nested is value:
            continue
        topology = topology_from_value(nested, depth + 1)
        if topology:
            return topology
    return None


def topology_from_step(step: dict) -> dict | None:
    return topology_from_value(step)


def thought_events(text: str) -> list[dict]:
    return [{"tag": "思考", "content": character} for character in text]


def tool_events(tool_name: str, running_text: str, result) -> list[dict]:
    result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return [
        {"tag": "tool_pending", "content": f"{tool_name}｜工具调用已创建，等待执行"},
        {"tag": "tool_running", "content": f"{tool_name}｜{running_text}"},
        {"tag": "tool_completed", "content": result_text},
    ]


def topology_tool_result(topology: dict) -> str:
    return json.dumps({"tag": "topology", "data": topology}, ensure_ascii=False)


def deep_diagnosis_scenario(execution: dict) -> dict:
    template_id = execution.get("template_id", "")
    scenarios = {
        "bgp-spine-flap": {
            "metrics": {"bgp_flap_count_30m": 7, "hold_timer_expired": 4, "interface_flap_count": 0, "cpu_peak_percent": 71},
            "hypotheses": [
                "物理链路异常假设：接口未发生 flap，证据不足",
                "对端进程重启假设：未发现设备重启记录，证据不足",
                "Keepalive 超时假设：Hold Timer 超时与邻居重建时间吻合，证据充分",
            ],
            "cause": "BGP Keepalive 报文间歇性超时导致邻居反复重建",
            "actions": ["核查两端 BGP 定时器、控制面策略和 CoPP 丢弃计数", "检查链路 MTU 与管理面 CPU 峰值是否和 flap 时间重合", "调整前先抓包确认 Keepalive 丢失方向，并观察至少 30 分钟"],
        },
        "ecmp-imbalance": {
            "metrics": {"path_1_percent": 61, "path_2_percent": 17, "path_3_percent": 13, "path_4_percent": 9, "member_link_down": 0},
            "hypotheses": ["成员链路故障假设：所有 ECMP 成员均为 up，证据不足", "带宽配置不一致假设：成员速率一致，证据不足", "哈希极化假设：五元组集中在单一成员链路，证据充分"],
            "cause": "业务五元组分布集中造成 ECMP 哈希极化",
            "actions": ["核对设备当前 ECMP 哈希字段配置", "抽样高流量业务的五元组分布", "低峰期评估增加哈希熵或调整业务分片策略"],
        },
        "buffer-pressure": {
            "metrics": {"egress_buffer_peak_percent": 92, "pause_frames_delta": 318, "tail_drop_delta": 86, "average_utilization_percent": 64},
            "hypotheses": ["持续带宽拥塞假设：平均利用率未持续超限，证据不足", "微突发假设：缓存峰值与 Tail Drop 同周期出现，证据充分", "硬件故障假设：其他端口缓存曲线正常，证据不足"],
            "cause": "高峰期业务微突发造成出方向缓存瞬时压满",
            "actions": ["定位产生微突发的入口端口和业务队列", "复核 QoS 队列、ECN 与 PFC 配置", "低峰期调整队列水位后持续观察 Tail Drop"],
        },
    }
    return scenarios.get(template_id, {
        "metrics": {"rx_power_dbm": -10.21, "crc_delta": 132, "loss_rate_percent": 2.73, "link_utilization_percent": 58},
        "hypotheses": ["拥塞假设：链路利用率未达到拥塞水位，证据不足", "控制面异常假设：未检测到邻居抖动或路径切换，证据不足", "物理链路异常假设：光功率下降、CRC 与丢包同步上升，证据充分"],
        "cause": "光模块或光纤链路衰减导致物理层误码与丢包",
        "actions": ["优先复核异常接口及其对端的物理状态", "清洁并复插相关光纤；指标未恢复时替换光模块或跳纤", "操作后连续观察丢包率、错误包和光功率至少 15 分钟"],
    })


def build_deep_diagnosis_report(execution: dict) -> str:
    scenario = deep_diagnosis_scenario(execution)
    actions = "\n".join(f"{index}. {action}" for index, action in enumerate(scenario["actions"], start=1))
    return f"""## 深度诊断结论

结合隐患分析报告、关联拓扑和实时遥测，**{execution['title']}** 的异常证据相互吻合，当前根因判断置信度为 **高**。

### 根因判断

- **{scenario['cause']}**
- 异常位于 `{execution['scope']}` 范围内，和隐患报告记录的影响范围一致
- 关键指标和异常发生时间相互吻合，其他主要假设的证据不足

### 处置建议

{actions}

### 诊断来源

本结论基于隐患执行 `{execution['id']}` 的分析报告进一步生成。
"""


def build_execution_diagnosis_steps(execution: dict, question: str) -> list[dict]:
    scenario = deep_diagnosis_scenario(execution)
    events: list[dict] = []
    events.extend(thought_events("我先读取本次隐患分析报告，确认已知现象、影响范围和初步根因，再决定需要补充哪些实时证据。"))
    events.extend(tool_events(
        "read_hazard_report",
        "正在加载隐患分析报告与执行上下文",
        {
            "execution_id": execution["id"],
            "title": execution["title"],
            "scope": execution["scope"],
            "report_loaded": bool(execution.get("report_markdown")),
        },
    ))
    events.extend(thought_events("报告信息已经加载。接下来关联影响拓扑，确认异常设备和异常链路是否与报告描述一致。"))
    events.extend(tool_events(
        "query_related_topology",
        "正在查询执行记录关联的设备与链路",
        topology_tool_result(execution.get("topology") or {"devices": [], "links": []}),
    ))
    events.extend(tool_events(
        "query_realtime_telemetry",
        "正在查询目标范围的实时指标",
        {
            "diagnosis_question": question,
            "execution_id": execution["id"],
            "scope": execution["scope"],
            "telemetry": scenario["metrics"],
        },
    ))
    hypothesis_summary = "；".join(scenario["hypotheses"])
    events.extend(thought_events(f"实时数据已经返回。我正在交叉验证根因假设：{hypothesis_summary}。综合证据后可以形成最终诊断结论。"))
    events.append({"tag": "最终结果", "content": build_deep_diagnosis_report(execution)})
    return events


def diagnosis_from_steps(steps: list[dict]) -> dict:
    topology = None
    for step in steps:
        topology = topology_from_step(step) or topology
    final_step = next((step for step in reversed(steps) if is_final_agent_event(step)), None)
    question = "基于这份隐患分析报告进一步诊断，确认根因并给出处置方案。"
    updated_at = now_text()
    return {
        "status": "completed" if final_step else "running",
        "question": question,
        "steps": deepcopy(steps),
        "turns": [{
            "id": f"turn-{uuid.uuid4().hex[:8]}",
            "question": question,
            "status": "completed" if final_step else "running",
            "steps": deepcopy(steps),
            "report_markdown": str(agent_event_content(final_step)) if final_step else None,
            "updated_at": updated_at,
        }],
        "topology": deepcopy(topology),
        "report_markdown": str(agent_event_content(final_step)) if final_step else None,
        "updated_at": updated_at,
    }


def mock_execution_diagnosis_events(execution: dict, question: str):
    for event in build_execution_diagnosis_steps(execution, question):
        time.sleep(MOCK_DELAY_SECONDS)
        yield event


HUB = PerceptionHub()
HUB.seed_completed(RISKS)


class DemoHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{now_text()}] {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def require_feature(self, name: str) -> bool:
        if feature_enabled(name):
            return True
        self.send_json({"error": "feature disabled", "feature": name}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/v1/") or path.startswith("/session") or path == "/health":
            self.proxy_backend()
            return
        if path == "/api/health":
            self.send_json({"ok": True, "service": "dcn-agent-demo"})
            return
        if path == "/api/config":
            self.send_json(load_config())
            return
        if path == "/api/risks":
            if not self.require_feature("perception"):
                return
            self.send_json(RISKS)
            return
        if path == "/api/executions":
            if not (feature_enabled("perception") or feature_enabled("deep_diagnosis")):
                self.send_json({"error": "feature disabled", "feature": "perception/deep_diagnosis"}, HTTPStatus.FORBIDDEN)
                return
            self.send_json(HUB.list_executions())
            return
        if path.startswith("/api/executions/") and path.endswith("/diagnosis"):
            if not self.require_feature("deep_diagnosis"):
                return
            execution_id = path.split("/")[3]
            execution = HUB.get_execution(execution_id)
            if execution is None:
                self.send_json({"error": "execution not found"}, HTTPStatus.NOT_FOUND)
                return
            if not execution.get("diagnosis"):
                self.send_json({"error": "diagnosis not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(execution["diagnosis"])
            return
        if path == "/api/perception/stream":
            if not self.require_feature("perception"):
                return
            self.stream_perception()
            return
        self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/v1/") or path.startswith("/session"):
            self.proxy_backend()
            return
        if path.startswith("/api/executions/") and path.endswith("/diagnose"):
            if not self.require_feature("deep_diagnosis"):
                return
            execution_id = path.split("/")[3]
            execution = HUB.get_execution(execution_id)
            if execution is None:
                self.send_json({"error": "execution not found"}, HTTPStatus.NOT_FOUND)
                return
            if not execution.get("report_markdown"):
                self.send_json({"error": "risk report is not ready"}, HTTPStatus.CONFLICT)
                return
            payload = self.read_json()
            question = str(payload.get("question") or "基于这份隐患分析报告进一步诊断，确认根因并给出处置方案。")
            self.stream_execution_diagnosis(execution, question)
            return
        if path.startswith("/api/risks/") and path.endswith("/dispatch"):
            if not self.require_feature("perception"):
                return
            risk_id = path.split("/")[3]
            risk = next((item for item in RISKS if item["id"] == risk_id), None)
            if risk is None:
                self.send_json({"error": "risk not found"}, HTTPStatus.NOT_FOUND)
                return
            execution = HUB.dispatch(risk)
            self.send_json({"ok": True, "execution": execution})
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        """转发用户和会话提示词设置接口。"""
        if urlparse(self.path).path.startswith("/v1/"):
            self.proxy_backend()
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def proxy_backend(self) -> None:
        """逐块转发后端原始 SSE；不缓冲整个回答，不向浏览器暴露服务令牌。"""
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json"), "X-User-ID": "local"}
        token = os.getenv(SETTINGS.server.api_token_env)
        if token:
            headers["Authorization"] = "Bearer " + token
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        started = False
        try:
            with httpx.Client(timeout=httpx.Timeout(30, read=SETTINGS.agent.max_run_seconds + 30)) as client:
                with client.stream(self.command, SETTINGS.frontend.backend_url.rstrip("/") + self.path,
                                   content=body, headers=headers) as response:
                    self.send_response(response.status_code)
                    for key in ("content-type", "x-session-id", "x-run-id", "content-disposition"):
                        if key in response.headers:
                            self.send_header(key, response.headers[key])
                    self.send_header("Connection", "close")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    started = True
                    for chunk in response.iter_bytes():
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except httpx.HTTPError as exc:
            if not started:
                self.send_json({"detail": f"后端连接失败：{exc}"}, 502)
        finally:
            self.close_connection = True

    def stream_execution_diagnosis(self, execution: dict, question: str) -> None:
        execution_id = execution["id"]
        HUB.begin_diagnosis(execution_id, question)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for item in mock_execution_diagnosis_events(execution, question):
                HUB.append_diagnosis_step(execution_id, item)
                data = json.dumps(item, ensure_ascii=False)
                self.wfile.write(f"event: agent\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            completed = HUB.get_execution(execution_id) or {}
            done = json.dumps(completed.get("diagnosis", {}), ensure_ascii=False)
            self.wfile.write(f"event: done\ndata: {done}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        self.close_connection = True

    def stream_perception(self) -> None:
        client = HUB.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        # The response itself remains open.  Mark the HTTP connection as close
        # so BaseHTTPRequestHandler will not attempt to read a second request
        # after an EventSource client disconnects.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = client.get(timeout=15)
                    data = json.dumps(payload, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            HUB.unsubscribe(client)
            self.close_connection = True

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            target = STATIC_DIR / "index.html"
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = SETTINGS.frontend.host
    port = SETTINGS.frontend.port
    server = ThreadingHTTPServer((host, port), DemoHandler)
    print(f"DCN demo is running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
