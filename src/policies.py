"""决策策略：真实模型（OpenAI 兼容接口）、脚本化假模型（测试用）、离线 Mock（只验证流程）。

三者实现同一个接口：给定对话消息和工具定义，返回下一步的工具调用。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Decision:
    content: str | None
    tool_calls: list[ToolCall]
    # 原样追加回对话的 assistant 消息（保留模型返回的全部字段）
    message: dict[str, Any] = field(default_factory=dict)


class Policy(Protocol):
    def decide(self, messages: list[dict], tools: list[dict]) -> Decision: ...


def _assistant_message(content: str | None, calls: list[ToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                },
            }
            for c in calls
        ],
    }


class LLMPolicy:
    """真实模型。不自动重试：失败直接抛出，由循环记为 error。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        from openai import OpenAI

        if not api_key:
            raise ValueError("缺少 LLM_API_KEY：请在 .env 中配置，或改用 --policy mock")
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)
        self.model = model

    def decide(self, messages: list[dict], tools: list[dict]) -> Decision:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=0
        )
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):
                # 不是 JSON 对象：交给工具层返回 INVALID_ARGS，让模型自己修正
                args = {"__raw__": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return Decision(msg.content, calls, msg.model_dump(exclude_none=True))


class ScriptedPolicy:
    """按预先写好的脚本逐轮返回工具调用。用于确定性测试守卫、上限与报告。"""

    def __init__(self, script: list[list[tuple[str, dict]]]):
        self.script = list(script)

    def decide(self, messages: list[dict], tools: list[dict]) -> Decision:
        if not self.script:
            return Decision(
                "（脚本已用完）", [], {"role": "assistant", "content": "（脚本已用完）"}
            )
        calls = [ToolCall(f"call_{uuid.uuid4().hex[:8]}", n, a) for n, a in self.script.pop(0)]
        return Decision(None, calls, _assistant_message(None, calls))
