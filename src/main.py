"""命令行入口。

    python -m src.main examples/01-normal/input.json                # 离线 Mock
    python -m src.main examples/01-normal/input.json --policy llm   # 真实模型（需 .env）

输出 JSON 报告（含 trace）。退出码：0=completed，2=needs_confirmation，1=其他未完成或失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import run_agent
from .config import Settings, load_dotenv
from .mock_policy import MockPolicy
from .policies import LLMPolicy
from .report import build_report
from .tools import ToolBox

EXIT_CODES = {"completed": 0, "needs_confirmation": 2}


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("须为正整数")
    return value


def load_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象")
    items = request.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("请求必须包含非空的 items 列表")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"items[{i}] 必须是对象")
        if not it.get("material_id") or not it.get("publish_at"):
            raise ValueError(f"items[{i}] 缺少 material_id 或 publish_at")
    return request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    parser.add_argument("input", type=Path, help="排期请求 JSON")
    parser.add_argument("--policy", choices=["mock", "llm"], default="mock")
    parser.add_argument("--out", type=Path, help="把报告写入文件")
    parser.add_argument("--max-steps", type=_positive_int)
    parser.add_argument("--max-tool-calls", type=_positive_int)
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings()
    if args.max_steps is not None:
        settings.max_steps = args.max_steps
    if args.max_tool_calls is not None:
        settings.max_tool_calls = args.max_tool_calls

    try:
        request = load_request(args.input)
    except (OSError, ValueError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 1

    if args.policy == "llm":
        try:
            policy = LLMPolicy(
                settings.llm_base_url,
                settings.llm_api_key,
                settings.llm_model,
                settings.llm_timeout,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        policy = MockPolicy(request)

    toolbox = ToolBox(request, settings.data_dir)
    run = run_agent(policy, toolbox, settings.max_steps, settings.max_tool_calls)
    report = build_report(request, toolbox.ledger, run)
    report["policy"] = args.policy if args.policy == "mock" else f"llm:{settings.llm_model}"

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return EXIT_CODES.get(report["task_status"], 1)


if __name__ == "__main__":
    sys.exit(main())
