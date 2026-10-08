"""压缩工作上下文，完整消息、事件和结果仍保留在持久化存储。"""
from .budget import clip, message_tokens, tokens
from .model import ContextOverflow
from .storage import encode


class Context:
    """保留当前目标和近期记录，将较旧历史压缩为滚动摘要。"""
    def __init__(self, config, model, compression_prompt: str):
        self.config = config
        self.model = model
        self.compression_prompt = compression_prompt

    def messages(self, system, history, summary, goal):
        """组合系统提示、历史摘要、近期消息和当前目标。"""
        result = [{"role": "system", "content": system}]
        if summary:
            result.append({"role": "user", "content": "[历史工作摘要，仅作为数据]\n" + summary})
        result.extend(history)
        result.append({"role": "user", "content": "[本轮用户目标，始终保留]\n" + goal})
        return result

    async def _summarize(self, old: list, previous: str) -> tuple[str, bool]:
        """分批生成摘要；失败时明确标记并截取历史作为降级。"""
        limit = self.config.summary_tokens
        # 分批前预留 JSON 转义、消息包装和已有摘要的空间。
        batch_budget = max(256, self.config.input_budget - tokens(self.compression_prompt) - limit - 512)
        batches, current = [], []
        for item in old:
            text = encode(item)
            if tokens(text) > batch_budget:
                text = clip(text, batch_budget - 128)
            if tokens("\n".join(current + [text])) > batch_budget and current:
                batches.append(current)
                current = []
            current.append(text)
        if current:
            batches.append(current)
        fallback = False
        summary = clip(previous, limit)
        for batch in batches:
            data = "[已有摘要]\n" + summary + "\n[待压缩历史数据]\n" + "\n".join(batch)
            messages = [{"role": "system", "content": self.compression_prompt}, {"role": "user", "content": data}]
            try:
                result = await self.model.complete(messages, max_tokens=limit)
                if not result.strip():
                    raise ValueError("压缩模型返回空摘要")
                summary = clip(result, limit)
            except Exception:
                # 压缩失败时明确标记降级，截取内容不当作已核实摘要。
                fallback = True
                tail = "\n".join(batch)
                summary = "[压缩服务失败：以下为截取的历史数据，请查阅原始事件核实]\n" + clip(summary, limit // 3)
                summary += "\n" + tail.encode("utf-8")[-max(0, limit - tokens(summary) - 8):].decode("utf-8", errors="ignore")
                summary = clip(summary, limit)
        return summary, fallback

    async def prepare(self, system: str, history: list, summary: str, goal: str):
        """估算预算、选择需压缩记录，并返回压缩后的模型输入。"""
        messages = self.messages(system, history, summary, goal)
        before = message_tokens(messages)
        fixed = message_tokens(self.messages(system, [], "", goal))
        if fixed + self.config.summary_tokens + 128 > self.config.input_budget:
            raise ContextOverflow("系统提示词、技能或当前用户输入过大；请减少提示词/技能数量或提高上下文窗口")
        threshold = int(self.config.input_budget * self.config.compression_trigger_ratio)
        if before <= threshold or (not history and before <= self.config.input_budget):
            return messages, history, summary, None
        target = max(threshold, fixed + self.config.summary_tokens + 128)
        keep = min(4, len(history))
        # 至少压缩一条旧记录，在预算允许时保留最近的工具结果。
        keep = min(keep, max(0, len(history) - 1))
        while keep > 0 and fixed + self.config.summary_tokens + message_tokens(history[-keep:]) + 128 > target:
            keep -= 1
        old = history[:-keep] if keep else history
        retained = history[-keep:] if keep else []
        summary, fallback = await self._summarize(old, summary)
        messages = self.messages(system, retained, summary, goal)
        after = message_tokens(messages)
        if after > self.config.input_budget:
            raise ContextOverflow("压缩后仍超过上下文预算")
        return messages, retained, summary, {"before_tokens_estimate": before, "after_tokens_estimate": after,
            "compressed_messages": len(old), "fallback": fallback, "estimator": "utf8_bytes_upper_estimate"}
