"""python chat_client.py '问题' --session-id 调用方的会话ID"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
from dcn_agent.config import load_settings

import httpx


def main():
    """发送用户问题和会话 ID，逐条显示后端的流式事件。"""
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
                      json={"message": args.message, "session_id": args.session_id}, timeout=180) as response:
        response.raise_for_status()
        print("session_id:", response.headers["X-Session-ID"])
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event["type"] == "message.part.delta":
                    print(event["properties"]["delta"], end="", flush=True)
                else:
                    print("\n" + json.dumps(event, ensure_ascii=False))


if __name__ == "__main__":
    main()
