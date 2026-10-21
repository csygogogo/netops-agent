# stdio MCP 服务目录

把本地 MCP 脚本放进本目录即完成注册，**无需改任何配置**：

- 目录中的每个 `*.py` 按文件名注册为同名服务：`mcp/nce_api.py` → 模型调用
  `mcp.call` 时填 `server: "nce_api"`；后端以子进程 + stdio 传输拉起脚本。
- 与 `[mcp.servers.*]` 的 HTTP 服务并存；同名时以 config.toml 中的显式配置优先。

## 脚本要求

1. 用独立 FastMCP 包编写（`from fastmcp import FastMCP`），工具用 `@mcp.tool()` 注册，
   入口直接 `mcp.run()`（默认 stdio 传输，不要传 `transport="http"` 等参数）。
   注意：mcp 2.x 已移除官方 SDK 内置的 `mcp.server.fastmcp`，后端已兼容 mcp 1.x/2.x，
   但脚本请统一使用独立 fastmcp 包。
2. 脚本常见的第三方依赖（`fastmcp`、`requests`、`paramiko`、`networkx`、`loguru` 等）
   已写入 requirements.txt，`pip install -r requirements.txt` 即可；脚本新增其他依赖时
   自行补充到后端环境。
3. 启动解释器默认是后端当前 Python，可在 `config.toml` 的 `[mcp].python_path` 指定。
4. stdio 模式下 stdout 是 JSON-RPC 协议通道：脚本里禁止任何 `print` 输出，
   调试信息一律写文件日志（如 loguru）。客户端虽能容忍少量杂散行，但会报解析错误。

## 示例骨架

```python
from fastmcp import FastMCP   # 独立 fastmcp 包；stdio 模式下禁止任何 print 输出

mcp = FastMCP("demo-stdio")

@mcp.tool(description="回显输入文本")
def echo(text: str) -> str:
    """示例工具"""
    return "echo:" + text

if __name__ == "__main__":
    mcp.run()
```

## 验证与生效

```bash
# 只列出某脚本的工具（不经过后端）
python -B -m dcn_agent.mcp_calling --script mcp/nce_api.py

# 启动后端后，经 /health 或模型工具 mcp.list_tools 确认注册
curl http://127.0.0.1:8520/health
```

新增或替换脚本后**重启后端**生效（目录在启动时扫描一次）。
每次工具调用会启动一个新的脚本子进程并在调用结束后回收；脚本导入较重时，
首次调用会有秒级启动开销，正常工具超时（`agent.tool_timeout_seconds`，默认 60 秒）
覆盖子进程启动、初始化和执行全过程。
