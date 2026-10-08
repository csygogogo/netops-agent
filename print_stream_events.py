"""透传打印 /v1/chat/stream 的 SSE 事件，用于核对 Agent 输出字段。

用法：python print_stream_events.py "问题" --session-id demo-evt
"""
import argparse
import os
import sys

sys.dont_write_bytecode = True
from dcn_agent.config import load_settings

import httpx


def main():
    """逐行原样输出 data: 之后的事件 JSON，不附加任何前缀或字段。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--url", help="后端地址，默认读取配置")
    args = parser.parse_args()
    settings = load_settings()
    url = (args.url or settings.frontend.backend_url).rstrip("/")
    headers = {"X-User-ID": "local"}
    token = os.getenv(settings.server.api_token_env)
    if token:
        headers["Authorization"] = "Bearer " + token
    with httpx.stream("POST", url + "/v1/chat/stream", headers=headers,
                      json={"message": args.message, "session_id": args.session_id}, timeout=600) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                print(line[6:], flush=True)


if __name__ == "__main__":
    main()
