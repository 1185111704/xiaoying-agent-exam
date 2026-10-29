"""ReAct 循环：模型选择动作 → 执行工具 → 观察结果 → 模型决定下一步或结束。

循环本身不做业务分支；守卫在工具层，拒绝原因作为观察返回给模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .policies import Policy
from .tools import TOOL_SPECS, ToolBox

SYSTEM_PROMPT = """你是内容团队的素材审核与发布排期助手。目标：为请求中的每项素材给出审核结论，并形成无冲突、可确认的排期草稿。

边界（必须遵守）：
- 你只能保存草稿，系统没有发布能力。任何时候都不要说"已发布"。
- 用户指定的发布时间如果不可用，可以建议替代时段，但必须标记 needs_confirmation=true 交给用户确认。
- fixed=true 的硬性要求不得擅自改动；无法满足时请求用户确认或阻断，并说明占用情况。
- 保存是否成功以 save_draft 的返回为准；失败就如实说明失败。

工作方式：
- 每次调用工具都在 reason 里写一句决策依据。
- 根据工具返回决定下一步：信息缺失时判断能否补查；规则命中时判断阻断还是请求确认；时段被占时查询替代时段；工具被拒绝时读懂原因再调整。
- 所有素材都有结论后调用 finish。"""


@dataclass
class RunResult:
    status: str  # finished / step_limit / tool_limit / error
    steps: int
    tool_calls: int
    trace: list[dict[str, Any]] = field(default_factory=list)
    final_message: str | None = None
    error: str | None = None


def _summary(result: dict[str, Any], limit: int = 300) -> str:
    text = json.dumps(result, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def run_agent(
    policy: Policy, toolbox: ToolBox, max_steps: int = 15, max_tool_calls: int = 30
) -> RunResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "排期请求：\n" + json.dumps(toolbox.request, ensure_ascii=False, indent=2),
        },
    ]
    run = RunResult(status="step_limit", steps=0, tool_calls=0)

    while run.steps < max_steps:
        if run.tool_calls >= max_tool_calls:
            run.status = "tool_limit"
            return run
        run.steps += 1
        try:
            decision = policy.decide(messages, TOOL_SPECS)
        except Exception as exc:  # 模型调用失败：不重试，如实返回
            run.status, run.error = "error", f"{type(exc).__name__}: {exc}"
            return run
        messages.append(decision.message)

        if not decision.tool_calls:
            # 模型没有调用工具：提醒一次并计入步数，由模型自己决定继续还是 finish
            run.final_message = decision.content
            messages.append(
                {"role": "user", "content": "请通过工具推进；所有素材都有结论后调用 finish。"}
            )
            continue

        for call in decision.tool_calls:
            if run.tool_calls >= max_tool_calls:
                run.status = "tool_limit"
                return run
            run.tool_calls += 1
            result = toolbox.call(run.steps, call.name, call.arguments)
            entry = toolbox.ledger.entries[-1]
            run.trace.append(
                {
                    "step": run.steps,
                    "tool": call.name,
                    "args": entry.args,
                    "reason": entry.reason,
                    "result": _summary(result),
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            if call.name == "finish" and result.get("ok"):
                run.status = "finished"
                run.final_message = entry.args.get("summary")
                return run
    return run
