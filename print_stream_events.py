"""按评测系统同款流程调用并透传打印事件。

POST /session 生成会话 → POST /session/{id}/prompt_async 异步提交（204）→
GET /event 收全局 SSE，输出本会话事件，收到 session.idle 结束。
不传 --session-id 时自动创建新会话（打印到 stderr），再次调用可传入该 ID 延续。
用法：python print_stream_events.py "问题"
"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
from dcn_agent.config import load_settings

import httpx


def main():
    """逐行原样输出 /event 的 data 事件，不附加任何前缀或字段。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--session-id", help="留空时自动调用 /session 生成新会话")
    parser.add_argument("--url", help="后端地址，默认读取配置")
    args = parser.parse_args()
    settings = load_settings()
    url = (args.url or settings.frontend.backend_url).rstrip("/")
    headers = {"X-User-ID": "local"}
    token = os.getenv(settings.server.api_token_env)
    if token:
        headers["Authorization"] = "Bearer " + token
    session_id = args.session_id or httpx.post(url + "/session", headers=headers, timeout=30).json()["session_id"]
    print(f"session_id: {session_id}", file=sys.stderr)  # 便于下一轮延续；不污染 stdout 的事件输出
    reply = httpx.post(url + f"/session/{session_id}/prompt_async", headers=headers,
                       json={"parts": [{"type": "text", "text": args.message}]}, timeout=30)
    reply.raise_for_status()
    watch = "ses_" + session_id
    with httpx.stream("GET", url + "/event", headers=headers, timeout=600) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            # 只输出本会话事件；总线级事件（connected/heartbeat）没有 sessionID。
            if event.get("properties", {}).get("sessionID") not in (None, watch):
                continue
            print(line[6:], flush=True)
            if event["type"] in ("session.idle", "session.error"):
                break


if __name__ == "__main__":
    main()
