# DCN 网络智能体

提供流式问答、ReAct 工具调用、Skill 渐进式加载、上下文压缩、多轮会话和拓扑定位。
对外 SSE 事件采用 opencode 风格协议（`{"id","type","properties"}`，会话/消息/分段生命周期），字段结构见 [后端手册](BACKEND.md)。
故障诊断接入真实模型与 MCP；前端"隐患感知"与"深度诊断"页面为模拟界面，可在 `frontend/config.json` 中关闭。

## 目录结构

```text
DCN_Agent/
├── run_server.py            # 后端启动入口（正式运行必需）
├── frontend/app.py          # 前端静态服务 + SSE 代理（正式运行必需）
├── chat_client.py           # 命令行聊天客户端，打印流式事件
├── print_stream_events.py   # SSE 事件核对工具，用于检查事件字段
├── config.toml              # 全部配置：模型、MCP、端口、技能、超时
├── requirements.txt         # Python 依赖（迁移后 pip install 即可）
├── dcn_agent/               # 后端核心包（正式运行必需）
│   ├── api.py               #   HTTP/SSE 接口层
│   ├── engine.py            #   ReAct 执行引擎（决策 → 工具 → 最终回答）
│   ├── events.py            #   事件协议转换：引擎内部事件 → opencode 风格对外事件
│   ├── model.py             #   模型适配：Ollama 原生 /api/chat 或 OpenAI 兼容接口
│   ├── llm_calling.py       #   OpenAI 兼容调用基础函数，可独立复用
│   ├── tools.py             #   本地工具路由（skill.* / mcp.* / artifact.*）
│   ├── mcp_calling.py       #   MCP Streamable HTTP 客户端，可独立复用
│   ├── skills.py            #   技能目录扫描与渐进式加载
│   ├── artifacts.py         #   工具结果落盘、字符分页与关键词检索
│   ├── context.py           #   上下文预算与滚动压缩
│   ├── budget.py            #   与 tokenizer 无关的保守 token 估算
│   ├── storage.py           #   SQLite 持久化与会话日志
│   ├── prompts.py           #   分层提示词加载
│   └── topology.py          #   拓扑快照查询（前端背景图）
├── prompts/                 # 提示词正文：system / controller / final / compress（必需）
├── skills/                  # 技能正文、参考文件与脚本（排障功能必需）
├── frontend/                # 前端：静态页面 + Python 代理（必需）
│   ├── config.json          #   功能开关：perception / deep_diagnosis（模拟页面）
│   └── static/              #   页面与逻辑；diagnosis.js 消费 opencode 风格事件
├── demo/                    # 【演示/Mock】联调演示专用，正式运行不需要，可不迁移
│   ├── demo_mcp_server.py   #   模拟 MCP 服务（无真实设备时的假数据）
│   ├── run_walkthrough.py   #   固定决策全流程演示（无模型时验证链路）
│   ├── walkthrough.toml     #   演示专用配置（独立端口与数据目录）
│   └── README.md            #   演示说明
├── data/                    # 【运行数据】已被 .gitignore 忽略，不要提交
│   ├── agent.sqlite3        #   会话/消息/事件数据库（首次运行自动创建）
│   └── sessions/<会话ID>/    #   session.log 与 results/ 完整工具结果
├── BACKEND.md               # 后端手册：事件协议、参数调整、接口明细
└── AGENTS.md                # 项目开发约定
```

## 迁移后如何运行（正式功能）

正式运行只需要 `dcn_agent/`、`frontend/`、`prompts/`、`skills/`、根目录入口脚本和 `config.toml`，
不依赖 `demo/` 目录（核心代码对它零引用），也不需要携带本地的 `data/`（运行时自动生成）。

1. **安装依赖**（Python 3.10+，任意 conda/venv 环境均可，不依赖环境名）：

   ```bash
   python -m pip install -r requirements.txt
   ```

2. **配置 `config.toml`**，改两处即可跑起来：

   - `[model]`：模型服务地址和名称。默认 Ollama（`transport = "ollama"`，`base_url` 填
     `http://<模型机>:11434/v1`）；模型服务提供 OpenAI 兼容接口时改 `transport = "openai"`。
   - `[mcp.servers.*]`：真实 MCP 的地址和工具白名单；没有 MCP 也能用普通问答和技能，
     只是排障工具不可用。前端拓扑图读取 `[topology]` 指定的三个查询工具。

3. **分别启动两个服务**（接入真实 MCP 时无需启动任何 demo 组件）：

   ```bash
   python run_server.py      # 后端，默认 0.0.0.0:8080
   python frontend/app.py    # 前端，默认 0.0.0.0:8090
   ```

   本机访问 http://127.0.0.1:8090 ，局域网设备访问 `http://服务器局域网IP:8090`。
   浏览器经前端代理连接后端，模型和 MCP 地址可继续使用服务器本机地址。

4. **验证**：

   ```bash
   curl http://127.0.0.1:8080/health                                   # 进程与配置检查
   python print_stream_events.py "解释一下 EVPN"   # 不传 --session-id 时自动经 /session 生成
   python chat_client.py "解释一下 EVPN" --session-id dcn-001          # 命令行对话
   ```

   `print_stream_events.py` 会按 `opencode_output_template.log` 同款格式逐条打印事件
   （默认折叠回答增量，加 `--full` 全量打印），可直接用于迁移后的字段核对。

命令行请求也支持多轮：相同 `--session-id` 延续历史。对外有两种调用模式，事件格式一致：

- **异步模式（opencode 客户端/评测系统）**：`POST /session` 获取 UUID（返回 `id` 字段）→
  `POST /session/{session_id}/prompt_async` 提交 `{"parts":[{"type":"text","text":"..."}]}`，
  返回 204 → `GET /event` 订阅全局 SSE 事件流，收到 `session.idle` 结束。
- **同步流式（本项目前端与命令行）**：`POST /v1/chat/stream`（body 含 `message` 和
  `session_id`），响应即 SSE。

事件协议与字段表见 [后端手册](BACKEND.md)。

## 演示模式（Mock，与正式功能隔离）

`demo/` 目录全部是联调演示，**与本机没有模型服务时的验证方式**：

```bash
python demo/run_walkthrough.py        # 固定决策 + 真实工具执行，覆盖技能/脚本/MCP/压缩/SSE
python demo/run_walkthrough.py --model # 换用 config.toml 里的真实模型自主排查
python demo/demo_mcp_server.py        # 单独启动模拟 MCP（配合 run_server.py 联调）
```

- 演示使用独立端口（18080/18090/18000）和独立数据目录 `data/demo/`，不影响正式服务。
- 固定决策模式（`WalkthroughModel`）**不代表大模型自主排障能力**，只用于链路验收。
- 迁移时如不需要演示，可不提交/不拷贝 `demo/` 目录；如已入库想移除：
  `git rm -r --cached demo/` 并在 `.gitignore` 中加入 `demo/`。

## 提交与迁移清单

| 内容 | 是否提交 | 说明 |
| --- | --- | --- |
| `dcn_agent/`、`frontend/`、`prompts/`、`skills/`、根目录脚本、`config.toml`、`requirements.txt`、文档 | 提交 | 正式功能全部依赖这些 |
| `data/` | 不提交 | 已被 `.gitignore` 忽略；含本地会话、测试产生的日志与 SQLite |
| `demo/` | 可选 | 演示/Mock 专用；核心零依赖，不想带 mock 就按上面命令移除 |
| 前端"隐患感知/深度诊断"页面 | 保留但可关闭 | 前端内置模拟界面，`frontend/config.json` 中 `features.perception=false`、`features.deep_diagnosis=false` 即隐藏，与后端无关 |

## 配置

配置集中在 `config.toml`，修改后重启对应服务。相对目录以配置文件所在目录为基准；
可通过 `DCN_CONFIG` 环境变量指定其他 TOML（自定义文件只需填写要覆盖的字段）。

| 配置项 | 用途 | 默认值 |
| --- | --- | --- |
| `server.host` / `server.port` | 后端监听地址、端口 | `0.0.0.0` / `8080` |
| `frontend.host` / `frontend.port` | 前端监听地址、端口 | `0.0.0.0` / `8090` |
| `frontend.backend_url` | 前端代理目标；空值自动跟随后端地址和端口 | `""` |
| `model.transport` | `ollama` 走原生 `/api/chat`（num_ctx 生效）；`openai` 走 `/v1` 兼容接口 | `ollama` |
| `model.base_url` / `model.model` | 模型服务地址、模型名称 | 本机 Ollama / `qwen2.5:3b` |
| `mcp.servers.<name>.url` | MCP 服务地址，名称用于 `mcp.call` 的 server 参数 | 无 |
| `mcp.servers.<name>.allowed_tools` | 工具白名单；空列表允许全部 | `[]` |
| `skills.python_path` | 技能脚本解释器；空值使用后端当前 Python | `""` |
| `topology.*` | 前端拓扑使用的 MCP 服务名与三个查询工具名 | demo 工具名 |

例如更换后端端口只需修改 `server.port`，前端和命令行客户端自动跟随。分机部署时填写
`frontend.backend_url`。Ollama 的监听端口由 Ollama 管理，本项目只配置连接地址。
Windows/Linux 均在目标 Python 环境中运行相同启动命令，无需填写本机解释器路径。

更多内容：[后端手册](BACKEND.md)（事件协议、上下文压缩参数、MCP、artifact 检索、API 索引）、
[前端说明](frontend/README.md)（事件展示、拓扑字段、样式入口）、[演示说明](demo/README.md)。
