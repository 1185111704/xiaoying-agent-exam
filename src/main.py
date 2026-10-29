"""命令行入口。

    python -m src.main examples/01-normal/input.json                # 离线 Mock
    python -m src.main examples/01-normal/input.json --policy llm   # 真实模型（需 .env）

输出 JSON 报告（含 trace）。退出码：0=completed，2=needs_confirmation，1=其他未完成或失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .agent import run_agent
from .config import Settings, load_dotenv
from .mock_policy import MockPolicy
from .policies import LLMPolicy
from .report import build_report
from .store import Store
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
    seen = set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"items[{i}] 必须是对象")
        mid, at = it.get("material_id"), it.get("publish_at")
        if not isinstance(mid, str) or not mid or not isinstance(at, str) or not at:
            raise ValueError(f"items[{i}] 缺少 material_id 或 publish_at")
        if mid in seen:
            raise ValueError(f"items[{i}] 素材 {mid} 重复出现")
        seen.add(mid)
        _check_time(at, f"items[{i}].publish_at")
        if "fixed" in it and not isinstance(it["fixed"], bool):
            raise ValueError(f"items[{i}].fixed 必须是布尔值")
        if "window" in it:
            w = it["window"]
            if not (isinstance(w, list) and len(w) == 2 and all(isinstance(x, str) for x in w)):
                raise ValueError(f"items[{i}].window 必须是 [开始, 结束] 两个时间字符串")
            for x in w:
                _check_time(x, f"items[{i}].window")
            if w[0] > w[1]:
                raise ValueError(f"items[{i}].window 开始时间晚于结束时间")
    return request


def _check_time(value: str, where: str) -> None:
    try:
        ok = datetime.strptime(value, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M") == value
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{where} 须为 YYYY-MM-DD HH:MM（补零），实际为 {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    parser.add_argument("input", type=Path, help="排期请求 JSON")
    parser.add_argument("--policy", choices=["mock", "llm"], default="mock")
    parser.add_argument("--out", type=Path, help="把报告写入文件")
    parser.add_argument(
        "--db", type=Path, help="状态库路径（默认 runtime/state.db，跨运行保留草稿）"
    )
    parser.add_argument("--data-dir", type=Path, help="合成数据目录（默认 data/），可替换后重跑")
    parser.add_argument(
        "--fault-save",
        choices=["always", "once"],
        help="故障注入：保存草稿时失败（always=每次，once=仅第一次），用于验收保存失败",
    )
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

    store = Store(args.db or settings.db_path, fault=args.fault_save)
    toolbox = ToolBox(request, args.data_dir or settings.data_dir, store)
    run = run_agent(policy, toolbox, settings.max_steps, settings.max_tool_calls)
    report = build_report(request, toolbox.ledger, run, toolbox.displaced)
    report["policy"] = args.policy if args.policy == "mock" else f"llm:{settings.llm_model}"

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return EXIT_CODES.get(report["task_status"], 1)


if __name__ == "__main__":
    sys.exit(main())
