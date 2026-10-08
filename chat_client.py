"""python chat_client.py '问题' --session-id 调用方的会话ID"""
import argparse
import json
import os

import httpx


def main():
    """发送用户问题和会话 ID，逐条显示后端的流式事件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    headers = {"X-User-ID": "local"}
    if os.getenv("DCN_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["DCN_API_TOKEN"]
    with httpx.stream("POST", args.url + "/v1/chat/stream", headers=headers,
                      json={"message": args.message, "session_id": args.session_id}, timeout=180) as response:
        response.raise_for_status()
        print("session_id:", response.headers["X-Session-ID"])
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event["type"] == "answer.delta":
                    print(event["data"]["text"], end="", flush=True)
                else:
                    print("\n" + json.dumps(event, ensure_ascii=False))


if __name__ == "__main__":
    main()
