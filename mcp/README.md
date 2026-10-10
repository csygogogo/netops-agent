# stdio MCP 服务目录

把本地 MCP 脚本放进本目录即完成注册，**无需改任何配置**：

- 目录中的每个 `*.py` 按文件名注册为同名服务：`mcp/nce_api.py` → 模型调用
  `mcp.call` 时填 `server: "nce_api"`；后端以子进程 + stdio 传输拉起脚本。
- 与 `[mcp.servers.*]` 的 HTTP 服务并存；同名时以 config.toml 中的显式配置优先。

## 脚本要求

1. 用 FastMCP 编写（独立包 `from fastmcp import FastMCP`，或官方 SDK 自带的
   `from mcp.server.fastmcp import FastMCP` 均可），工具用 `@mcp.tool()` 注册，
   入口直接 `mcp.run()`（默认 stdio 传输，不要传 `transport="http"` 等参数）。
2. 脚本自身的第三方依赖（如 `fastmcp`、`paramiko`、`networkx`、`loguru`）必须装在
   后端同一 Python 环境中——这些依赖不写入项目 requirements.txt，由脚本提供方维护。
3. 启动解释器默认是后端当前 Python，可在 `config.toml` 的 `[mcp].python_path` 指定。
4. 客户端容忍脚本在 stdout 的少量杂散输出（如调试 `print`），但请避免高频打印。

## 示例骨架

```python
from mcp.server.fastmcp import FastMCP   # 或 from fastmcp import FastMCP

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
