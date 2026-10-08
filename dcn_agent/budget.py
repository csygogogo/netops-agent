"""使用与具体模型 tokenizer 无关的保守预算估算。"""
import json


def tokens(text: str) -> int:
    # 按字节估算，比字符数除以 4 更保守，适合中文及日志。
    """用 UTF-8 字节数保守估算 token，避免中文和日志被低估。"""
    return len(text.encode("utf-8"))


def clip(text: str, budget: int) -> str:
    """按预算截取文本，避免截断 UTF-8 字符并附截取标记。"""
    if tokens(text) <= budget:
        return text
    marker = "\n[截取，更多内容请按需读取]"
    available = max(0, budget - tokens(marker))
    return text.encode("utf-8")[:available].decode("utf-8", errors="ignore") + (marker if budget >= tokens(marker) else "")


def message_tokens(messages: list[dict]) -> int:
    """累计消息文本和每条消息的协议开销估算。"""
    return sum(16 + tokens(json.dumps(m, ensure_ascii=False, separators=(",", ":"))) for m in messages)
