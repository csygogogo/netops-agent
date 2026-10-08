"""启动后端；Windows 和 Linux 均使用当前已激活的 conda agent 环境。"""
import sys

# 标准入口不生成 __pycache__，在导入项目模块前设置。
sys.dont_write_bytecode = True

import uvicorn

from dcn_agent.api import create_app
from dcn_agent.config import load_settings


if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port, access_log=False)
