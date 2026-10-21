import sys

import os
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)
# 注意：stdio MCP 模式下 stdout 是 JSON-RPC 协议通道，禁止任何 print 输出；
# 调试信息一律走 logger（写入 mcp_server.log）。
import re
import json
import time
import threading
import signal
import socket
import atexit
from datetime import datetime
from collections import defaultdict
from loguru import logger

LOG_FILE = os.path.join(current_dir, "mcp_server.log")
logger.add(
    LOG_FILE,
    rotation="50 MB",      # 日志文件达到 50MB 时自动轮转
    retention="7 days",    # 保留 7 天的日志
    level="INFO",          # 日志级别
    encoding="utf-8",      # 编码格式
    enqueue=True,          # 线程安全，异步写入
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}"
)
import traceback
import asyncio
from fastmcp import FastMCP
from typing import Annotated, List, Dict, Any, Optional
from pydantic import Field
from fastapi import HTTPException
import base64
import textwrap
import paramiko
import networkx as nx

# ============================================================
# API 调用统计模块
# 功能：
#   1. 统计每个接口的调用次数（成功 / 失败）
#   2. 统计每个接口的平均耗时（毫秒）
#   3. 记录最近一次失败的错误信息
#   4. 持久化到 JSON 文件，服务重启后可恢复
#   5. 通过 MCP 工具 get_api_statistics 主动查询
# ============================================================

STATS_FILE = os.path.join(current_dir, "api_call_stats.json")


def list_to_multiline(data):
    """把 data 列表转换为多行文本。

    针对 api 返回的 data 列表，若元素是 JSON 对象/数组（或 JSON 字符串），
    会进行格式化缩进，使其跨越多行 —— 避免拼接后形成单条超长记录，
    导致 opencode 的 grep 工具报 "Ripgrep JSON record exceeded 65536 bytes"。
    """
    if not isinstance(data, list):
        return data
    lines = []
    for item in data:
        if item is None:
            continue
        # 元素本身是 dict/list -> 格式化多行
        if isinstance(item, (dict, list)):
            try:
                lines.append(json.dumps(item, ensure_ascii=False, indent=2))
                continue
            except Exception:
                lines.append(str(item))
                continue
        # 字符串：尝试按 JSON 解析后格式化；解析失败则原样
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, (dict, list)):
                        lines.append(json.dumps(obj, ensure_ascii=False, indent=2))
                        continue
                    else:
                        lines.append(item)
                        continue
                except Exception:
                    lines.append(item)
                    continue
            continue  # 空字符串跳过
        lines.append(str(item))
    return '\n'.join(lines)


class APICallStats:
    """线程安全的 API 调用统计器"""

    def __init__(self):
        self.lock = threading.Lock()
        # { tool_name: { "success": 0, "failure": 0, "total_time_ms": 0 } }
        self.stats: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"success": 0, "failure": 0, "total_time_ms": 0.0}
        )
        # 记录最近一次失败信息：{ tool_name: { "error": str, "timestamp": str } }
        self.last_error: Dict[str, Dict[str, str]] = {}
        self.start_time = datetime.now().isoformat()
        self._load_from_file()

    def _load_from_file(self):
        """从文件恢复历史统计数据"""
        try:
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for tool_name, s in data.get("stats", {}).items():
                    self.stats[tool_name].update(s)
                self.last_error.update(data.get("last_error", {}))
                logger.info(f"[API统计] 从文件恢复历史统计数据，共 {len(data.get('stats', {}))} 个接口")
        except Exception as e:
            logger.warning(f"[API统计] 从文件恢复历史数据失败: {e}")

    def _save_to_file(self):
        """将统计数据持久化到文件"""
        try:
            data = {
                "start_time": self.start_time,
                "stats": dict(self.stats),
                "last_error": self.last_error
            }
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[API统计] 保存统计数据到文件失败: {e}")

    def record_success(self, tool_name: str, elapsed_ms: float):
        """记录一次成功的 API 调用"""
        with self.lock:
            self.stats[tool_name]["success"] += 1
            self.stats[tool_name]["total_time_ms"] += elapsed_ms

    def record_failure(self, tool_name: str, error_msg: str, elapsed_ms: float):
        """记录一次失败的 API 调用"""
        with self.lock:
            self.stats[tool_name]["failure"] += 1
            self.stats[tool_name]["total_time_ms"] += elapsed_ms
            self.last_error[tool_name] = {
                "error": str(error_msg)[:500],  # 截断避免过长
                "timestamp": datetime.now().isoformat()
            }

    def get_summary(self) -> Dict[str, Any]:
        """获取统计摘要"""
        with self.lock:
            summary = {
                "start_time": self.start_time,
                "total_calls": 0,
                "total_success": 0,
                "total_failure": 0,
                "tools": []
            }
            for tool_name, s in sorted(self.stats.items()):
                total = s["success"] + s["failure"]
                if total == 0:
                    continue
                avg_ms = s["total_time_ms"] / total if total > 0 else 0
                entry = {
                    "tool_name": tool_name,
                    "total": total,
                    "success": s["success"],
                    "failure": s["failure"],
                    "failure_rate": f'{(s["failure"] / total * 100):.2f}%',
                    "avg_time_ms": round(avg_ms, 2)
                }
                if tool_name in self.last_error:
                    entry["last_error"] = self.last_error[tool_name]
                summary["tools"].append(entry)
                summary["total_calls"] += total
                summary["total_success"] += s["success"]
                summary["total_failure"] += s["failure"]
            return summary

    def export_log(self) -> str:
        """生成格式化的统计日志文本"""
        summary = self.get_summary()
        lines = [
            "=" * 70,
            "          MCP API 调用统计报告",
            f"  服务启动时间: {summary['start_time']}",
            f"  统计生成时间: {datetime.now().isoformat()}",
            "=" * 70,
            "",
            f"  总调用次数:   {summary['total_calls']}",
            f"  总成功次数:   {summary['total_success']}",
            f"  总失败次数:   {summary['total_failure']}",
            f"  总体成功率:   {(summary['total_success']/summary['total_calls']*100) if summary['total_calls']>0 else 0:.2f}%" if summary['total_calls'] > 0 else "  总体成功率:   0%",
            "",
            "-" * 70,
            f"  {'接口名称':<45} {'调用次数':>6}  {'成功':>6}  {'失败':>6}  {'失败率':>8}  {'平均耗时':>10}",
            "-" * 70
        ]
        for t in summary["tools"]:
            lines.append(
                f"  {t['tool_name']:<45} {t['total']:>6}  {t['success']:>6}  "
                f"{t['failure']:>6}  {t['failure_rate']:>8}  {t['avg_time_ms']:>9.2f}ms"
            )
        lines.append("-" * 70)

        # 失败详情
        failed_tools = [t for t in summary["tools"] if t["failure"] > 0]
        if failed_tools:
            lines.append("")
            lines.append("  【调用失败的接口详情】")
            lines.append("")
            for t in failed_tools:
                if "last_error" in t:
                    lines.append(f"  - {t['tool_name']}")
                    lines.append(f"    失败次数: {t['failure']}")
                    lines.append(f"    最后失败: {t['last_error']['error']}")
                    lines.append(f"    失败时间: {t['last_error']['timestamp']}")
                    lines.append("")

        lines.append("=" * 70)
        return "\n".join(lines)

    def persist(self):
        """手动触发持久化"""
        self._save_to_file()


# 全局统计器实例
api_stats = APICallStats()
# add
# from fi.odae.rest_client import RestClient  # 不再使用，改为SSH远程调用
import requests
import urllib3
import urllib.parse
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# SSH远程API调用配置
SSH_HOST = '7.246.111.189'
SSH_PORT = 22
SSH_USERNAME = 'root'
SSH_PASSWORD = 'Changeme_123'
SCRIPT_PATH = '/home/ossadm/api_call_script.py'

# 加载Mock配置
def load_mock_config():
    config_path = os.path.join(current_dir, "mock_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("enable", False)
    except Exception:
        return False

MOCK_ENABLED = load_mock_config()

API_CONFIG = {
    "query_device_link": {
        "url": "/rest/uninetwork-res/v1/ai/link/instances",
        "method": "POST",
        "description": "查询设备链路信息"
    }
}

mcp = FastMCP(name="NCE API工具集（合并版）",
              instructions="NCE网络设备管理API工具集，包含设备链路查询、风险查询等13个路由工具")

# 路径到 cot_api_info.json (使用相对路径)
COT_API_INFO_FILE = os.path.join(project_root, "fi_api", "cot_api_info.json")

def _load_output_schema(url: str) -> Dict[str, Any]:
    """
    从 cot_api_info.json 加载指定URL对应的 outputSchema

    Args:
        url: API的URL路径，如 "/rest/uninetwork-res/v1/ai/port/instances"

    Returns:
        outputSchema 字典，如果未找到则返回空字典
    """
    try:
        with open(COT_API_INFO_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        tools = data.get('tools', [])
        for item in tools:
            if 'annotations' in item and 'toolExtend' in item['annotations']:
                if item['annotations']['toolExtend'].get('URL') == url:
                    return item.get('outputSchema', {})
        return {}
    except Exception as e:
        logger.warning(f"加载 outputSchema 失败: {e}")
        return {}

def _filter_data_by_allowed_fields(data: List[Dict], allowed_fields: List[str]) -> List[Dict]:
    """
    根据允许的字段列表过滤数据

    Args:
        data: 原始数据列表
        allowed_fields: 允许的字段名列表

    Returns:
        过滤后的数据列表
    """
    if not allowed_fields:
        return data

    filtered_data = []
    for item in data:
        if isinstance(item, dict):
            filtered_item = {k: v for k, v in item.items() if k in allowed_fields}
            filtered_data.append(filtered_item)
        else:
            filtered_data.append(item)
    return filtered_data

def _get_allowed_fields_from_schema(schema: Dict[str, Any]) -> List[str]:
    """
    从 outputSchema 中提取允许的字段名

    Args:
        schema: outputSchema 字典

    Returns:
        允许的字段名列表，如果返回空列表表示不过滤数据
    """
    # 优先查找 aiInstanceData 的 childParams（用于设备/资源类API）
    ai_instance_data = schema.get('aiInstanceData', {})
    if isinstance(ai_instance_data, dict) and 'childParams' in ai_instance_data:
        child_params = ai_instance_data['childParams']
        if child_params and isinstance(child_params, list):
            # 第一个对象字段的 childParams 包含实际的字段定义
            if isinstance(child_params[0], dict) and 'childParams' in child_params[0]:
                field_params = child_params[0]['childParams']
                if isinstance(field_params, list):
                    return [field.get('paramName') for field in field_params if field.get('paramName')]

    # 查找 telemetryData 的 childParams（用于指标类API）
    telemetry_data = schema.get('telemetryData', {})
    if isinstance(telemetry_data, dict) and 'childParams' in telemetry_data:
        child_params = telemetry_data['childParams']
        if child_params and isinstance(child_params, list):
            if isinstance(child_params[0], dict) and 'childParams' in child_params[0]:
                field_params = child_params[0]['childParams']
                if isinstance(field_params, list):
                    return [field.get('paramName') for field in field_params if field.get('paramName')]

    return []

def filter_result_by_output_schema(tool_name: str, result: Dict[str, Any], data_key: str = None, total_key: str = None) -> Dict[str, Any]:
    """
    根据 outputSchema 过滤工具返回结果

    Args:
        tool_name: 工具名称，对应 API_CONFIG 中的 key
        result: API 返回的原始结果
        data_key: 数据字段的键名（可选），如果为None则自动检测 "aiInstanceData" 或 "telemetryData"
        total_key: 总数字段的键名（可选），如果为None则自动检测

    Returns:
        过滤后的结果，如果不需要过滤则返回原结果
    """
    try:
        # 获取工具配置中的 URL
        config = API_CONFIG.get(tool_name)
        if not config or "url" not in config:
            return result

        url = config["url"]
        output_schema = _load_output_schema(url)
        if not output_schema:
            return result

        # 获取允许的字段列表
        allowed_fields = _get_allowed_fields_from_schema(output_schema)
        if not allowed_fields:
            return result

        # 自动检测数据字段键名
        if data_key is None:
            if "aiInstanceData" in result:
                data_key = "aiInstanceData"
                total_key = total_key or "aiInstanceTotal"
            elif "telemetryData" in result:
                data_key = "telemetryData"
                total_key = total_key or "telemetryTotal"
            else:
                return result
        elif total_key is None:
            total_key = "aiInstanceTotal" if data_key == "aiInstanceData" else "telemetryTotal"

        # 过滤数据
        if data_key in result:
            data = result[data_key]
            if isinstance(data, list):
                filtered_data = _filter_data_by_allowed_fields(data, allowed_fields)
                result[data_key] = filtered_data
                # 更新总数字段
                if total_key in result:
                    result[total_key] = len(filtered_data)
                logger.info(f"[{tool_name}] 根据outputSchema过滤数据({data_key}): {allowed_fields}, 过滤前{len(data)}, 过滤后{len(filtered_data)}")

        return result
    except Exception as e:
        logger.warning(f"[{tool_name}] 根据outputSchema过滤数据失败: {e}")
        return result

# client = RestClient()  # 不再使用，改为SSH远程调用

# ============================================================
# 远端 HTTP 代理配置（替代 SSH 远程执行，解决并发/脚本覆盖问题）
# 对应远端服务：remote_api_proxy.py（常驻运行于 10.137.16.124）
# ============================================================
PROXY_URL = "http://7.246.111.189:32020/api-proxy"


def call_api(tool_name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    统一的API调用函数（通过远端HTTP代理执行，带调用统计埋点）

    本地直接 HTTP 请求远端常驻代理，代理内部每请求新建 CommonHttpClient 实例
    （独立 lock+conn）从而真正并行。相较原 SSH 方案：
      - 省去 SSH 握手/su 切换/脚本拷贝/进程启动，延迟大幅降低
      - 消除单一共享 SCRIPT_PATH 的并发覆盖写问题
    旧 SSH 实现保留在 call_api_ssh_legacy，可作为 fallback。
    """
    start = time.time()

    def _finalize(success: bool, error_msg: str = ""):
        elapsed_ms = round((time.time() - start) * 1000, 2)
        if success:
            api_stats.record_success(tool_name, elapsed_ms)
            logger.info(f"[API统计] {tool_name}: success | cost: {elapsed_ms}ms")
        else:
            api_stats.record_failure(tool_name, error_msg, elapsed_ms)
            logger.warning(f"[API统计] {tool_name}: failure | cost: {elapsed_ms}ms | error: {error_msg[:100]}")

    if params is None:
        params = {}

    config = API_CONFIG.get(tool_name)
    if not config:
        err = f"Tool {tool_name} not found"
        _finalize(False, err)
        raise HTTPException(status_code=404, detail=err)

    method_raw = config["method"]
    method = method_raw[3:] if method_raw.startswith("IR_") else method_raw
    uri = config["url"]

    # GET 拼 query，POST 传 body
    if method == "GET":
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{uri}?{qs}" if qs else uri
        body = None
    else:
        url = uri
        body = params

    try:
        logger.info(f"url: {url}, body: {body}")
        resp = requests.post(
            PROXY_URL,
            json={"method": method, "url": url, "body": body},
            headers={"Content-Type": "application/json"},
            timeout=120,
            proxies={"http": None, "https": None},  # 绕开公司代理，直连内网代理服务
        )
    except requests.exceptions.Timeout:
        _finalize(False, "proxy timeout (120s)")
        raise HTTPException(status_code=504, detail="proxy timeout (120s)")
    except Exception as e:
        _finalize(False, f"proxy call failed: {e}")
        raise HTTPException(status_code=500, detail=f"proxy call failed: {e}")

    try:
        result = resp.json()
    except Exception:
        result = {"raw_response": resp.text}

    if resp.status_code >= 400:
        err = f"HTTP {resp.status_code}: {json.dumps(result, ensure_ascii=False)[:500]}"
        _finalize(False, err)
        raise HTTPException(status_code=500, detail=err)

    # 保留原有约定：data 列表转多行文本（JSON 元素格式化，避免单行超长）
    if isinstance(result.get('data'), list):
        result['data'] = list_to_multiline(result['data'])

    _finalize(True)
    return result


def call_api_ssh_legacy(tool_name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    旧版 统一的API调用函数（通过SSH远程执行，带调用统计埋点）
    保留作为 fallback：通过paramiko SSH连接远程执行脚本调用CommonHttpClient。
    """
    start = time.time()

    def _finalize(success: bool, error_msg: str = ""):
        elapsed_ms = round((time.time() - start) * 1000, 2)
        if success:
            api_stats.record_success(tool_name, elapsed_ms)
            logger.info(f"[API统计] {tool_name}: success | cost: {elapsed_ms}ms")
        else:
            api_stats.record_failure(tool_name, error_msg, elapsed_ms)
            logger.warning(f"[API统计] {tool_name}: failure | cost: {elapsed_ms}ms | error: {error_msg[:100]}")

    if params is None:
        params = {}

    config = API_CONFIG.get(tool_name)
    if not config:
        err = f"Tool {tool_name} not found"
        _finalize(False, err)
        raise HTTPException(status_code=404, detail=err)

    uri = config["url"]
    method_raw = config["method"]

    # 提取HTTP方法：IR_GET->GET, IR_POST->POST, IR_GET/IR_POST前缀去掉
    if method_raw.startswith("IR_"):
        method = method_raw[3:]  # 去掉IR_前缀
    else:
        method = method_raw

    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USERNAME,
            password=SSH_PASSWORD,
            timeout=30
        )

        body_p = {json.dumps(params, ensure_ascii=False)}
        print(body_p)
        
        full_url = uri
        if method == "GET":
            query_string = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            full_url = f"{uri}?{query_string}" if query_string else uri
            print(full_url)

        body_json = json.dumps(params, ensure_ascii=False)
        body_b64 = base64.b64encode(body_json.encode()).decode()
        body_data = json.loads(base64.b64decode(body_b64).decode())
        print(f"body: {body_data}")
        
        script_content = f'''import json
import base64
from util.httpclient import CommonHttpClient
full_url = "{full_url}"
url = "{uri}"
method = "{method}"
body = json.loads(base64.b64decode('{body_b64}').decode())
headers = {{"Content-Type": "application/json"}}

api_client = CommonHttpClient('10.137.16.124', 32018, True, False, headers=headers)

if method == "GET":
    status_code, response_text = api_client.get(full_url)
elif method == "POST":
    status_code, response_text = api_client.post(url, body)
else:
    print(json.dumps({{"status_code": 500, "text": "Unsupported method"}}))
    exit(0)

# Ensure response_text is a string for JSON serialization
if isinstance(response_text, bytes):
    response_text = response_text.decode(encoding='utf-8')

print(json.dumps({{"status_code": status_code, "text": response_text}}, ensure_ascii=False))
'''

        # 编写脚本到远程服务器
        write_cmd = f'cat > {SCRIPT_PATH} << \'SCRIPTEOF\'\n{script_content}\nSCRIPTEOF'
        stdin, stdout, stderr = client.exec_command(write_cmd)
        stdout.read()
        stderr.read()

        # 执行脚本
        cmd = f'su - ossadm bash -c \'. /opt/*/manager/agent/bin/engr_profile.sh; python {SCRIPT_PATH}\''
        stdin, stdout, stderr = client.exec_command(cmd)
        
        # 设置通道超时，如 120 秒
        stdout.channel.settimeout(120)
        stderr.channel.settimeout(120)

        try:
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
        except socket.timeout:
            error_detail = "Remote command execution timeout (120s)"
            _finalize(False, error_detail)
            raise HTTPException(status_code=504, detail=error_detail)

        if output:
            remote_result = json.loads(output)
            status_code = remote_result.get("status_code", 500)
            response_text = remote_result.get("text", "")

            if status_code >= 400:
                error_detail = f"HTTP {status_code}: {response_text[:500]}"
                _finalize(False, error_detail)
                raise HTTPException(status_code=500, detail=error_detail)

            if response_text:
                try:
                    result = json.loads(response_text)
                    # todo: 解析result中的data，把“,”分割改成用“\n”分割，然后再拼接起来
                    if isinstance(result.get('data'), list):
                        # result['raw_data'] = result['data']    # 原始数据为 raw_data
                        result['data'] = list_to_multiline(result['data'])  # data 转换为多行文本（JSON 格式化）
                except json.JSONDecodeError:
                    result = {"raw_response": response_text}
                _finalize(True)
                return result
            else:
                error_detail = "Empty response from remote API"
                _finalize(False, error_detail)
                raise HTTPException(status_code=500, detail=error_detail)
        else:
            error_detail = f"Remote execution failed: {error[:500]}"
            _finalize(False, error_detail)
            raise HTTPException(status_code=500, detail=error_detail)

    except HTTPException:
        raise
    except Exception as e:
        err = f"Remote API call failed: {str(e)}"
        _finalize(False, err)
        raise HTTPException(status_code=500, detail=err)
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

# ==================== 工具1: 查询设备链路信息 ====================
@mcp.tool(name="queryDeviceLink", description="查询设备链路信息。支持按设备名称、设备IP、接口名称、链路状态筛选")
def queryDeviceLink(
        filter: Annotated[Optional[List[Dict[str, Any]]],
        Field(description="筛选条件列表。每个条件包含：\n"
                          "- key: 字段名，可选值：neName（设备名称）、neIp（设备IP）、ifName（接口名称）、linkStatus（链路状态）\n"
                          "- operate: 操作符，可选值：eq（精确匹配）、like（模糊匹配）\n"
                          "- value: 筛选值\n\n"
                          "链路状态(linkStatus)的可选值：active（正常）、inactive（未激活）、unknown（未知）、maintenance（维护中）\n\n"
                          "示例：\n"
                          '[{"key": "neName", "operate": "eq", "value": "switch-01"}, '
                          '{"key": "linkStatus", "operate": "eq", "value": "active"}]')] = None,
        pageNo: Annotated[Optional[int], Field(description="分页页码")] = None,
        tenantId: Annotated[Optional[str], Field(description="租户ID，UUID格式")] = None,
        pageSize: Annotated[Optional[int], Field(description="分页大小")] = None) -> str:
    """查询设备链路信息"""
    try:
        # 构建参数
        params = {"pageNo": pageNo, "pageSize": pageSize, "tenantId": tenantId, "filter": filter}

        params = {k: v for k, v in params.items() if v is not None}

        # 调用API
        result = call_api("query_device_link", params)
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error(f"查询设备链路失败: {e}")
        return json.dumps({"error_code": 500, "error_msg": str(e), "status_code": 500}, ensure_ascii=False)


# ==================== 工具1: 查询设备路由表项 ====================
@mcp.tool(name="queryDeviceRoutingEntry", description="根据 设备ID 查询设备路由表项内容，此工具只用于查询公网路由表项，私网请用工具 send_telnet_command")
def queryDeviceRoutingEntry(
    ne_dn: Annotated[str, Field(description="根据 设备ID 查询公网设备路由表项内容")],
    cmd_list: Annotated[List[str], Field(description="根据 设备ID 查询公网设备路由表项内容")] = ["display ip routing-table", "display ip routing-table verbose"]):
        try:
            resp = _do_send_telnet_command(ne_dn=ne_dn, cmd_list=cmd_list, isAsync=False)
            print(f"device_routing_entry log: {resp}")
            return resp
        except Exception as e:
            logger.error(f"根据 设备ID 查询公网设备路由表项内容失败: {e}")
            return json.dumps({"error_code": 500, "error_msg": str(e), "status_code": 500}, ensure_ascii=False)

def _print_stats_on_exit():
    """服务退出时记录统计报告（走日志文件，不占用 stdout 协议通道）"""
    try:
        report = api_stats.export_log()
        logger.info("退出统计报告：\n" + report)
        api_stats.persist()
    except Exception:
        pass


def _run_self_test():
    """联通性自测：直接调用 call_api 验证远端 HTTP 代理是否连通。
    不经由 MCP 框架，直接执行真实调用（只读），用于开发/部署验证。
    """
    print(">>> 测试 query_ip_subnets (GET)...")
    try:
        r1 = call_api("query_ip_subnets", {"pageNo": 1, "pageSize": 10})
        print("GET result keys:", list(r1.keys())[:10] if isinstance(r1, dict) else type(r1))
        print("GET result preview:", json.dumps(r1, ensure_ascii=False)[:1024])
    except Exception as e:
        print("GET call FAILED:", repr(e))

    print(">>> 测试 query_device_link (POST)...")
    try:
        r2 = call_api("query_device_link", {"pageNo": 1, "pageSize": 5})
        print("POST result keys:", list(r2.keys())[:10] if isinstance(r2, dict) else type(r2))
        print("POST result preview:", json.dumps(r2, ensure_ascii=False)[:1024])
    except Exception as e:
        print("POST call FAILED:", repr(e))
        
    print(">>> 测试 queryPeList (POST)...")
    try:
        r3 = call_api("queryPeList", {"dstIp":"100.10.1.100", "endTime":1790607150823, "startTime":1790606550823, "srcIp":"100.210.1.10"})
        print("POST result keys:", list(r3.keys())[:10] if isinstance(r3, dict) else type(r3))
        print("POST result preview:", json.dumps(r3, ensure_ascii=False))
    except Exception as e:
        print("POST call FAILED:", repr(e))


if __name__ == "__main__":
    # ============================================================
    # 启动 MCP 服务 / 或执行联通性自测
    #   启动服务：  python mcp_template.py（由 DCN_Agent 后端按 stdio 自动拉起，无需手动启动）
    #   自测连通：  python mcp_template.py --test
    # call_api 已改为走远端 HTTP 代理（remote_api_proxy.py），
    # 并绕过本机公司代理直连内网。
    # ============================================================
    if "--test" in sys.argv:
        _run_self_test()
        sys.exit(0)

    atexit.register(_print_stats_on_exit)

    # 注册信号处理：优雅退出时打印统计
    def _signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，准备退出...")
        _print_stats_on_exit()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info(f"API调用统计文件: {STATS_FILE}")
    
    logger.info("启动MCP服务，共28个工具")
    mcp.run()