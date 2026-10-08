# DCN 网络智能体后端

已实现 FastAPI + SSE 后端、ReAct 工具循环、Skill 渐进式加载、上下文压缩、超长结果检索、多轮会话和分层提示词。需要 Python 3.11+。

```powershell
conda activate agent
python -m pip install -r requirements.txt
python run_server.py
```

前端故障诊断已接入真实 SSE：运行 `python demo/demo_mcp_server.py` 和 `python frontend/app.py`，打开 http://127.0.0.1:8090 。完整说明见 [frontend/README.md](frontend/README.md)。隐患界面暂保留模拟流程。

完整功能演示：运行 `python demo/run_walkthrough.py`，打开 http://127.0.0.1:18090 ，点击“多 Pod · 主备链路双故障”，可观察拓扑定位、技能、脚本、长日志检索、模拟修复和复查。默认采用固定决策、实际执行工具；加 `--model` 使用配置中的真实模型。场景与数据目录说明见 [demo/README.md](demo/README.md)。

另开终端：

```powershell
python chat_client.py "你好，请介绍你能做什么" --session-id "demo-001"
```

- 默认接口：`POST http://127.0.0.1:8080/v1/chat/stream`
- 请求必传 `session_id`，首次使用自动创建，相同 ID 延续会话。
- 配置：`config.toml`
- **完整后端使用说明、事件协议、六项功能说明见 [BACKEND.md](BACKEND.md)。**

下面是两个可独立复用的基础调用模块的说明。

## 目录与日志

```text
run_server.py              # 服务启动入口
chat_client.py             # 命令行聊天客户端
demo/                     # 本地联调用的模拟 MCP 服务
dcn_agent/
  llm_calling.py           # 可独立导入的模型调用函数
  mcp_calling.py           # 可独立导入的 MCP 调用函数
  engine.py               # 智能体循环，其余模块见 BACKEND.md
skills/                   # 技能及按需加载的资源
prompts/                  # 系统和阶段提示词
data/
  agent.sqlite3           # 会话历史、摘要、提示词和运行状态
  sessions/<session_id>/
    session.log           # 一个会话一个日志，多轮追加
    results/              # 完整工具结果 JSON 和检索文本
```

`session.log` 每行是一条 JSON，记录用户消息、决策说明、工具调用、工具结果片段和回答。
每条包含原始 `session_id`；用 `run_id` 区分同一会话的不同轮次。超长原文放在同目录的 `results/`，通过 `artifact_id` 对应。
目录直接使用调用方的 session_id，便于定位。例如会话 dcn-001 的日志位于 data/sessions/dcn-001/session.log。为兼容 Windows/Linux，ID 只允许字母、数字及 _ . -，不能以点开头或结尾，不能使用 CON、NUL、COM1 等 Windows 保留名称，也不能与已有 ID 仅大小写不同。
SQLite 用于程序读取会话状态，日志用于人排查问题。进程启动和 HTTP 服务异常仍输出到终端。

不保留 `tests/`、`__init__.py` 或测试日志。Python 3.11+ 支持当前目录作为命名空间包；在项目根目录导入 `dcn_agent` 即可。
`python run_server.py` 已关闭字节码缓存；独立执行模块可用下面示例中的 `-B`，同样不生成 `__pycache__`。

## Linux 迁移

复制项目源码、配置、技能和提示词；如需保留会话，停服后一起复制整个 `data/`。
目标机器使用 Python 3.11+ 的 conda `agent` 环境，首次没有此环境时先执行 `conda create -n agent python=3.12`。

```bash
conda activate agent
python -m pip install -r requirements.txt
# 按实际部署地址配置；本机服务才使用 localhost
export MCP_SERVER_URL="http://localhost:8000/mcp"
export OLLAMA_BASE_URL="http://localhost:11434/v1"
python run_server.py
```

配置文件中的相对目录以该配置文件所在目录为基准。Skill 脚本使用 `skills.python_path` 指定的 Python（留空沿用后端解释器），不依赖 Windows 命令。输出直接落盘，长结果按需检索。

`skills.python_path` 默认为空，技能脚本自动使用启动后端的 Python。切换机器或 Python 环境时，无需修改此配置；进入目标环境后执行 `python run_server.py` 即可。只有需要让技能脚本使用另一个 Python 环境时，才在 `config.toml` 中填写该字段。
VS Code 中选择本机的 `agent` 解释器即可，项目不再保存个人电脑的解释器绝对路径。

## 安装

在项目目录执行（Windows PowerShell）：

```powershell
conda activate agent
python -m pip install -r requirements.txt
```

后续命令均在 `conda activate agent` 后运行。

## MCP：Streamable HTTP

使用完整的 MCP 服务地址（路径由服务端决定）：

```powershell
$env:MCP_SERVER_URL = "http://localhost:8000/mcp"
# 如果服务需要 Bearer Token：
# $env:MCP_API_TOKEN = "你的 Token"
python -B -m dcn_agent.mcp_calling
```

以上地址只是示例，需要替换为真实地址。直接运行脚本会列出工具名称、描述和参数 schema。
也可以通过函数的 `server_url=`、`headers=` 显式传配置；显式请求头覆盖同名默认请求头。
脚本读取进程环境变量，不自动加载 `.env` 文件。

在其他脚本中调用：

```python
import asyncio
import json
from dcn_agent.mcp_calling import multi_mcp_calling

async def main():
    res = await multi_mcp_calling([
        {"name": "queryXX", "param": {"paraname1": "value1", "paraname2": "value2"}}
    ])
    print(json.dumps(res, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
```

`queryXX`、参数名和参数值是占位示例，请用服务端真实工具替换。
每个输入对应一个返回项，顺序不变，重复调用同名工具不会覆盖结果：

```json
[
  {
    "name": "queryXX",
    "ok": true,
    "text": "工具返回的文本",
    "data": {"status": "正常"},
    "content": [{"type": "text", "text": "工具返回的文本"}],
    "error": null
  }
]
```

- `data` 保存服务返回的 `structuredContent`，未提供时为 `None`；不会猜测文本的 JSON 含义。
- `content` 保留全部内容块，包括图片、资源等非文本结果；`text` 只拼接文本块。
- 工具错误、调用超时或连接失败通过 `ok=False`、`error` 返回；输入/配置错误抛出 `ValueError`。
- 默认 `timeout=60` 秒，限制初始化和每次工具调用（不含并发排队时间）；并非整个批次总时限。
- 默认 `max_concurrency=1`，单项失败仍继续后续项。多个独立查询可传 `max_concurrency=4`。
- 一个批次共用一个 MCP 会话；本接口调用同一服务的多个工具，不做跨服务自动路由。
- 不自动重试。超时/断连仅代表客户端未取得结果，不能据此判断服务端操作没有执行。
- `await list_mcp_tools()` 可取得工具及输入 schema；它的连接错误直接向调用方抛出。

## Ollama 大模型

默认配置与你提供的一致：

```text
base_url = http://localhost:11434/v1
api_key = ollama
model = qwen2.5:3b
```

确保 Ollama 已运行且已拉取 `qwen2.5:3b`。直接运行默认流式打印：

```powershell
python -B -m dcn_agent.llm_calling "请介绍一下 DCN 网络故障排查"
python -B -m dcn_agent.llm_calling "test" --no-stream
```

同步调用：

```python
from contextlib import closing
from dcn_agent.llm_calling import call_llm, stream_llm

messages = [
    {"role": "system", "content": "你是 DCN 网络运维助手。"},
    {"role": "user", "content": "test"},
]
answer = call_llm(messages)
print(answer)

parts = []
with closing(stream_llm(messages, temperature=0.2)) as chunks:
    for chunk in chunks:
        print(chunk, end="", flush=True)
        parts.append(chunk)
print()
full_answer = "".join(parts)
```

与异步 MCP 集成时使用异步接口：

```python
import asyncio
from contextlib import aclosing
from dcn_agent.llm_calling import acall_llm, astream_llm

async def main():
    answer = await acall_llm("test")
    print(answer)
    async with aclosing(astream_llm("test")) as chunks:
        async for chunk in chunks:
            print(chunk, end="", flush=True)
    print()

if __name__ == "__main__":
    asyncio.run(main())
```

四个函数都接受字符串或 `messages` 列表，可显式设置 `model`、`base_url`、`api_key`、
`timeout`（默认 120 秒，HTTP 超时，不是整段生成的总时限），及兼容的 `temperature`、`max_tokens` 等参数。
也可设置 `OLLAMA_BASE_URL`、`OLLAMA_API_KEY`、`OLLAMA_MODEL` 环境变量。
连接/模型错误保留 OpenAI SDK 原始异常，便于上层处理；不自动重试，流式中断不会重新生成并重复输出。
流式函数返回文本增量，不自动打印、不存储对话历史，也不自动执行模型输出的工具调用。
如提前 `break`，请像示例一样用 `closing` / `aclosing` 及时释放连接。

## 接口参考

- [OpenAI Docs：Chat Completions 流式事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
- [MCP 官方 Python SDK v1 客户端文档](https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/docs/client.md)

依赖限定 MCP SDK 1.x，以匹配当前脚本采用的 `ClientSession` 接口。
