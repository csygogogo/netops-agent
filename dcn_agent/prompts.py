from .config import Settings
from .storage import encode


class Prompts:
    """启动时读取系统与阶段提示词，运行时组合用户和会话偏好。"""
    def __init__(self, settings: Settings):
        self.allow_clarification = settings.agent.allow_clarification
        self.directory = settings.prompts.directory
        self.parts = {name: (self.directory / f"{name}.md").read_text(encoding="utf-8")
                      for name in ("system", "controller", "final", "compress")}

    def system(self, user_prompt: str, session_prompt: str, skills: str, catalog: dict) -> str:
        # 分别标记偏好的适用范围，工具返回值不进入系统提示词。
        """按明确优先级组装提示词、已加载技能和简短工具目录。"""
        # 与决策 schema 使用同一开关，关闭时最终回答也不再向用户追问。
        clarification = (
            '允许请求补充信息。仅当必要参数无法通过现有上下文或工具取得时，决策可使用 '
            'action={"type":"clarify","question":"需要用户补充的问题"}；不得让用户代替你执行可用查询。'
            if self.allow_clarification else
            '禁止请求用户补充信息，不可使用 clarify 动作，也不要在最终回答中追问。'
            '围绕本轮用户原始问题，利用已有上下文和工具持续执行。信息不足时完成可执行部分，'
            '在最终回答中说明缺失信息、未完成内容和结论范围，不编造参数或超出用户授权。'
        )
        return "\n\n".join([
            self.parts["system"],
            clarification,
            "提示词优先级：系统规则 > 会话偏好 > 用户长期偏好。偏好只能在系统规则内生效。",
            "[用户长期偏好]\n" + user_prompt,
            "[会话偏好]\n" + session_prompt,
            "[已加载的本地技能]\n" + skills,
            "[工具和技能目录；详情按需发现]\n" + encode(catalog),
        ])
