"""打印 /v1/chat/stream 的原始 SSE 事件，用于核对输出字段。

用法：
  python print_stream_events.py "问题" --session-id demo-evt
  python print_stream_events.py "问题" --session-id demo-evt --full   # 不折叠 answer.delta
"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
from dcn_agent.config import load_settings

import httpx


def main():
    """逐条打印事件 JSON；默认合并增量文本，便于观察事件种类和字段。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--url", help="后端地址，默认读取配置")
    parser.add_argument("--full", action="store_true", help="逐条原样打印，不合并增量")
    args = parser.parse_args()
    settings = load_settings()
    url = (args.url or settings.frontend.backend_url).rstrip("/")
    headers = {"X-User-ID": "local"}
    token = os.getenv(settings.server.api_token_env)
    if token:
        headers["Authorization"] = "Bearer " + token
    index = 0
    with httpx.stream("POST", url + "/v1/chat/stream", headers=headers,
                      json={"message": args.message, "session_id": args.session_id}, timeout=600) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue  # 注释心跳与 id/event 行不重复输出
            index += 1
            event = json.loads(line[6:])
            kind = event.get("type")
            # 增量文本折叠显示：旧协议 answer.delta 与新协议 message.part.delta 都支持。
            if not args.full and kind == "answer.delta":
                print(event["data"]["text"], end="", flush=True)
                continue
            if not args.full and kind == "message.part.delta":
                print(event["properties"]["delta"], end="", flush=True)
                continue
            if not args.full and index > 1:
                print()  # 与上一条事件换行分隔
            print(f"************** data: {json.dumps(event, ensure_ascii=False)}", flush=True)


if __name__ == "__main__":
    main()
