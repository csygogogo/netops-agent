"""配置默认值只保留在 config.toml；这里负责读取和基本检查。"""
import math
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录，不依赖启动时的工作目录
Settings = SimpleNamespace


class ModelConfig(SimpleNamespace):
    """模型配置，集中计算可用于输入的上下文预算。"""
    @property
    def input_budget(self):
        """窗口减去输出预留和安全余量，得到输入预算。"""
        return self.context_window_tokens - self.max_output_tokens - self.safety_margin_tokens


def load_settings(path=None):
    """读取默认配置和覆盖项，将相对目录解析到配置文件旁。"""
    default_path = ROOT / "config.toml"
    path = Path(path or os.getenv("DCN_CONFIG", default_path)).resolve()
    with default_path.open("rb") as stream:
        data = tomllib.load(stream)
    data.setdefault("mcp", {"servers": {}})

    # 自定义文件只需填写要覆盖的字段。
    if path != default_path:
        with path.open("rb") as stream:
            overrides = tomllib.load(stream)
        for section, values in overrides.items():
            if section not in data or not isinstance(values, dict):
                raise ValueError(f"未知配置段: {section}")
            for key, value in values.items():
                if key not in data[section]:
                    raise ValueError(f"未知配置项: {section}.{key}")
                data[section][key] = value

    # 保留运行所需的基本检查，不再重复声明每一层配置模型。
    for section, values in data.items():
        for key, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                zero_allowed = key in {"temperature", "decision_retries"}
                if not math.isfinite(value) or value < 0 or (value == 0 and not zero_allowed):
                    raise ValueError(f"无效配置值: {section}.{key}")

    settings = Settings(**{name: SimpleNamespace(**values) for name, values in data.items()})
    settings.model = ModelConfig(**data["model"])
    if not isinstance(settings.agent.allow_clarification, bool):
        raise ValueError("agent.allow_clarification 必须为 true 或 false")
    model = settings.model
    if model.transport not in {"ollama", "openai"}:
        raise ValueError("model.transport 应为 ollama 或 openai")
    if model.input_budget < 2048 or model.summary_tokens > model.input_budget // 3:
        raise ValueError("上下文预算过小，或 summary_tokens 超过输入预算的三分之一")
    if not 0 < model.compression_trigger_ratio <= 1:
        raise ValueError("compression_trigger_ratio 应在 0..1 之间")
    if settings.agent.tool_result_tokens < 256:
        raise ValueError("tool_result_tokens 至少为 256")

    for section in (settings.storage, settings.skills, settings.prompts):
        section.directory = (path.parent / section.directory).resolve()
    if not isinstance(settings.skills.allow_scripts, bool):
        raise ValueError("skills.allow_scripts 必须为 true 或 false")
    if not isinstance(settings.skills.python_path, str):
        raise ValueError("skills.python_path 必须为 Python 文件路径或空字符串")
    if settings.skills.python_path:
        # 相对解释器路径也以配置文件所在目录为基准。
        python_path = (path.parent / Path(settings.skills.python_path).expanduser()).resolve()
        if not python_path.is_file():
            raise ValueError(f"Python 文件不存在: {python_path}")
        settings.skills.python_path = str(python_path)
    model.base_url = os.getenv("OLLAMA_BASE_URL", model.base_url)
    model.model = os.getenv("OLLAMA_MODEL", model.model)

    servers = settings.mcp.servers
    if not servers and os.getenv("MCP_SERVER_URL"):
        servers["default"] = {"url": os.environ["MCP_SERVER_URL"], "token_env": "MCP_API_TOKEN"}
    settings.mcp.servers = {}
    for name, server in servers.items():
        if not server.get("url") or set(server) - {"url", "token_env", "allowed_tools"}:
            raise ValueError(f"MCP 服务 {name} 的配置无效")
        settings.mcp.servers[name] = SimpleNamespace(
            url=server["url"], token_env=server.get("token_env"), allowed_tools=server.get("allowed_tools", []))
    return settings
