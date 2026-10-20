"""使用与具体模型 tokenizer 无关的保守预算估算。"""
import json
import re


# CJK 部首、汉字、注音、兼容表意文字、中文标点和全角符号。
_CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯]")


def tokens(text: str) -> int:
    """按字符类型加权估算 token：中文约 1 token/字，其他约 0.4 token/字符。

    早期版本按 UTF-8 字节数估算，中文被高估 3 倍以上，单轮排障频繁误触发
    上下文压缩。常见 tokenizer 中文实际约 0.5..0.7 token/字、ASCII 约 3..4
    字符/token，本估算仍整体偏保守，且始终满足 tokens(text) <= len(text)。
    """
    cjk = len(_CJK.findall(text))
    return cjk + ((len(text) - cjk) * 2 + 4) // 5  # 非 CJK 按 0.4 token/字符向上取整


def clip(text: str, budget: int) -> str:
    """按预算截取文本，以字符为单位缩短并附截取标记。"""
    if tokens(text) <= budget:
        return text
    marker = "\n[截取，更多内容请按需读取]"
    room = budget - tokens(marker)
    if room <= 0:  # 预算连标记都放不下时只保留内容
        room, marker = max(1, budget), ""
    limit = min(len(text), room)  # tokens(text[:x]) <= x，从 room 起最多回退标记长度
    while limit > 0 and tokens(text[:limit]) > room:
        limit -= 1
    return text[:limit] + marker


def message_tokens(messages: list[dict]) -> int:
    """累计消息文本和每条消息的协议开销估算。"""
    return sum(16 + tokens(json.dumps(m, ensure_ascii=False, separators=(",", ":"))) for m in messages)
