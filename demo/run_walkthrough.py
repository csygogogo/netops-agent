"""启动独立全流程演示；--model 切换为真实大模型自主排查。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from weakref import WeakKeyDictionary

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dcn_agent.api import create_app
from dcn_agent.config import load_settings

QUESTION = "请运行多 Pod 双故障完整演示：加载 dcn-path-investigation 技能，读取参考流程并创建独立 DEMO 实例。排查 checkout 主备路径，执行计数器分析脚本，检索并读取长日志证据。授权仅对该 DEMO 实例执行模拟修复，然后复查接口、计数器和业务主备状态，给出报告。"


class WalkthroughModel:
    """固定决策只用于链路验收；工具、脚本、压缩和 SSE 均走实际后端。"""
    def __init__(self):
        self.runs = WeakKeyDictionary()  # 每个后台执行任务单独计步，支持多会话并发。

    def state(self):
        return self.runs.setdefault(asyncio.current_task(), {"step": 0, "metrics": [], "observations": [], "case_id": ""})

    def observe(self, observation, state):
        """只读取本轮真实工具结果，动态取得 case_id、采样与日志位置。"""
        if not state["step"]:
            return
        if not observation or not observation.get("ok"):
            raise ValueError("演示工具失败或结果缺失，停止流程；请查看上一张工具卡片")
        if state.get("last_call") == observation["call_id"]:
            raise ValueError("未取得本步骤的新工具结果")
        state["last_call"] = observation["call_id"]
        state["observations"].append(observation)
        raw = observation.get("result", {})
        data = raw.get("data") if isinstance(raw, dict) else None
        if data is None and isinstance(raw, dict) and raw.get("text"):
            try:
                data = json.loads(raw["text"])
            except ValueError:
                data = None
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if isinstance(data, dict):
            if data.get("case_id"):
                state["case_id"] = data["case_id"]
            if "samples" in data:
                state["samples"] = data["samples"]
            if "assessment" in data:
                state["metrics"].append(data)
            if "primary_healthy" in data:
                state["service"] = data
        # 日志检索继续使用日志的 ID，不使用检索结果自身的 artifact_id。
        if state["step"] == 12:
            state["log_id"] = observation["artifact_id"]
        if observation["name"] == "artifact.search":
            matches = raw.get("matches", [])
            if not matches:
                raise ValueError("模拟日志中未找到预期证据")
            state["offset"] = matches[0]["offset"]

    async def complete(self, messages, json_mode=False, max_tokens=None):
        state = self.state()
        if not json_mode:
            # 演示摘要也注明来源；真实模型模式由正常 Model 生成摘要。
            return f"固定步骤演示，已执行 {state['step']} 步；实例 {state['case_id']}。完整证据保存于本会话工具结果。"
        # 固定入口仅接受按钮中的完整授权样例，不能把“不要修复”等其他问题误当作授权。
        goal = messages[-1]["content"].removeprefix("[本轮用户目标，始终保留]\n").strip()
        if not state["step"] and goal != QUESTION:
            state["help"] = True
            return json.dumps({"plan": [], "summary": "此端口仅提供固定流程演示", "reflection": "", "action": {"type": "final"}}, ensure_ascii=False)
        case = state["case_id"]

        def tool(tool_name, **arguments):
            return {"type": "tool", "name": tool_name, "arguments": arguments}

        def mcp(name, **arguments):
            return tool("mcp.call", server="demo", name=name, arguments=arguments)

        def counter_script():
            return tool("skill.run", name="dcn-path-investigation", script="scripts/analyze_counters.py",
                        args=["--samples", json.dumps(state["samples"]), "--threshold", "0.01"])

        steps = [
            ("加载跨 Pod 故障技能", lambda: tool("skill.load", name="dcn-path-investigation")),
            ("按需读取参考流程", lambda: tool("skill.read", name="dcn-path-investigation", path="references/procedure.md")),
            ("发现模拟 MCP 工具", lambda: tool("mcp.list_tools", server="demo", limit=10)),
            ("创建独立故障实例", lambda: mcp("start_demo_case")),
            ("定位业务主备路径", lambda: mcp("get_service_path", case_id=case)),
            ("记录修复前业务状态", lambda: mcp("get_service_status", case_id=case)),
            ("核对主接入接口并定位 POD-02", lambda: mcp("get_interface_status", case_id=case, device="Leaf-03", interface="Ethernet1/49")),
            ("核对备用接入接口的健康状态", lambda: mcp("get_interface_status", case_id=case, device="Leaf-04", interface="Ethernet1/50")),
            ("取得备用接口前后两次采样", lambda: mcp("get_interface_counters", case_id=case, device="Leaf-04", interface="Ethernet1/50")),
            ("在本机执行计数器增量分析脚本", counter_script),
            ("核对目的 Pod 的接口", lambda: mcp("get_interface_status", case_id=case, device="Leaf-05", interface="Ethernet1/49")),
            ("取得主接口完整单行历史日志", lambda: mcp("get_device_logs", case_id=case, device="Leaf-03")),
            ("在完整日志中检索光信号异常", lambda: tool("artifact.search", artifact_id=state["log_id"], terms=["loss_of_signal"], limit=1, radius=120)),
            ("读取命中位置附近的原文", lambda: tool("artifact.read", artifact_id=state["log_id"], offset=state["offset"], limit=400)),
            ("模拟更换主接口光模块", lambda: mcp("repair_demo_link", case_id=case, device="Leaf-03", interface="Ethernet1/49", action="replace_optic")),
            ("模拟更换备用接口线缆", lambda: mcp("repair_demo_link", case_id=case, device="Leaf-04", interface="Ethernet1/50", action="replace_cable")),
            ("复查主接口，确认异常标记消除", lambda: mcp("get_interface_status", case_id=case, device="Leaf-03", interface="Ethernet1/49")),
            ("复查备用接口健康状态", lambda: mcp("get_interface_status", case_id=case, device="Leaf-04", interface="Ethernet1/50")),
            ("重新获取修复后的计数器采样", lambda: mcp("get_interface_counters", case_id=case, device="Leaf-04", interface="Ethernet1/50")),
            ("再次运行脚本核对 CRC 增量", counter_script),
            ("核实业务主备路径与成功率", lambda: mcp("get_service_status", case_id=case)),
        ]
        index = state["step"]
        if index >= len(steps):
            if not state.get("service", {}).get("primary_healthy") or not state["service"].get("backup_healthy"):
                raise ValueError("主备业务验证未通过")
            if len(state["metrics"]) != 2 or state["metrics"][-1]["error_rate"] != 0:
                raise ValueError("脚本复查未通过")
            action, title = {"type": "final"}, "汇总实际工具与脚本返回的验证结果"
        else:
            title, build = steps[index]
            action = build()
        state["step"] += 1
        await asyncio.sleep(1)  # 演示节奏便于观察定位切换，不影响真实模型入口。
        return json.dumps({"plan": ["技能与业务路径", "接口、脚本和日志证据", "模拟修复与复查"] if index == 0 else [],
            "summary": title, "reflection": "上一工具已返回；按演示检查表继续核对证据。" if index else "",
            "action": action}, ensure_ascii=False)

    async def stream(self, messages):
        state = self.state()
        if state.get("help"):
            yield "当前为固定步骤演示入口。请点击“多 Pod · 主备链路双故障”，输入需明确授权 DEMO 模拟修复。普通问答请使用正式入口，或以 --model 启动真实模型模式。"
            return
        before, after = state["metrics"]
        service = state["service"]
        text = (f"## DEMO 全流程验证完成\n\n实例：`{state['case_id']}`。本入口使用固定决策，所有工具和脚本均实际执行，不代表大模型自主排障能力。\n\n"
                f"| 核对项 | 修复前 | 复查结果 |\n| --- | --- | --- |\n"
                f"| Leaf-03 / Ethernet1/49 | down，日志命中 loss_of_signal | 主路径健康：{service['primary_healthy']} |\n"
                f"| Leaf-04 / Ethernet1/50 | up，CRC 错误率 {before['error_percent']}% | CRC 错误率 {after['error_percent']}% |\n"
                f"| checkout 业务 | 主备路径异常 | {service['successes']}/{service['requests']} 次请求成功，主备均已验证 |\n\n"
                f"已模拟更换光模块和线缆。没有操作真实设备。日志证据：`{state['log_id']}`，字符 offset `{state['offset']}`。")
        for start in range(0, len(text), 24):
            yield text[start:start + 24]
            await asyncio.sleep(.04)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="store_true", help="使用配置中的实际大模型，不使用固定步骤")
    parser.add_argument("--backend", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = ROOT / "demo/walkthrough.toml"
    settings = load_settings(config)
    if args.backend:
        import uvicorn
        if not args.model:
            settings.model.model = "DEMO 固定决策 · 实际工具执行"
        model = None if args.model else WalkthroughModel()
        app = create_app(settings, model=model)
        if model:
            # 固定控制器直接接收真实工具事件，历史压缩不能抹掉演示计步所需的数据。
            original_event = app.state.agent.event
            def event(state, event_type, data):
                payload = original_event(state, event_type, data)
                if event_type == "tool.result":
                    model.observe(data, model.state())
                return payload
            app.state.agent.event = event
        uvicorn.run(app, host="127.0.0.1", port=18080, access_log=False)
        return
    for port in (18000, 18080, 18090):
        with socket.socket() as sock:
            sock.settimeout(.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 已占用，请先关闭此前的演示进程")
    env = dict(os.environ, DCN_CONFIG=str(config), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    commands = [
        ["-c", "from demo.demo_mcp_server import server; server.settings.port=18000; server.run(transport='streamable-http')"],
        [str(Path(__file__).resolve()), "--backend", *(["--model"] if args.model else [])],
        ["frontend/app.py"],
    ]
    children = []
    try:
        for command in commands:
            children.append(subprocess.Popen([sys.executable, "-B", *command], cwd=ROOT, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        print("演示进程 PID（MCP / 后端 / 前端）：" + " / ".join(str(child.pid) for child in children), flush=True)
        print("演示界面：http://127.0.0.1:18090\n模式：" + ("真实大模型" if args.model else "固定步骤 / 实际工具") + "\n输入：" + QUESTION + "\n按 Ctrl+C 停止全部演示服务。", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()


if __name__ == "__main__":
    main()
