"""一键运行全部示例，输出写入 examples/<用例>/output.<policy>[.<run>].json。

    python scripts/run_examples.py --policy mock   # 离线，无需 key
    python scripts/run_examples.py --policy llm    # 真实模型，需要 .env

每个用例使用独立的状态库（runtime/examples/<用例>.db），运行前清空，结果可复现。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.main import main  # noqa: E402


def _schedule_edit(add=(), remove=()):
    def apply(data_dir: Path) -> None:
        path = data_dir / "schedule.json"
        sched = [e for e in json.loads(path.read_text()) if e["material_id"] not in remove]
        sched += [
            {"account": "douyin_main", "slot": s, "material_id": m, "title": t} for s, m, t in add
        ]
        path.write_text(json.dumps(sched, ensure_ascii=False, indent=2))

    return apply


# (用例, [(本次运行的标签, 额外参数, 运行前对数据的修改)])
CASES = [
    ("01-normal", [("", [], None)]),
    ("02-missing-info", [("", [], None)]),
    ("03-rule-risk", [("", [], None)]),
    ("04-conflict", [("", [], None)]),
    ("05-duplicate", [("run1", [], None), ("run2", [], None)]),
    (
        "06-save-failure",
        [("always", ["--fault-save", "always"], None), ("once", ["--fault-save", "once"], None)],
    ),
    (
        "07-resubmit",
        [
            ("run1", [], None),
            ("run2-19-taken", [], _schedule_edit(add=[("2026-11-03 19:00", "X40", "临时插播")])),
            ("run3-18-freed", [], _schedule_edit(remove=["X21"])),
        ],
    ),
    ("08-step-limit", [("", ["--max-steps", "2"], None)]),
]


def run_case(name: str, runs, policy: str) -> list[tuple[str, int, dict]]:
    db = ROOT / "runtime" / "examples" / f"{name}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        shutil.copytree(ROOT / "data", data)
        for label, extra, edit in runs:
            if edit:
                edit(data)  # 修改累积生效：模拟排期随时间变化
            suffix = f".{label}" if label else ""
            out = ROOT / "examples" / name / f"output.{policy}{suffix}.json"
            argv = [
                str(ROOT / "examples" / name / "input.json"),
                "--policy",
                policy,
                "--db",
                str(db),
                "--data-dir",
                str(data),
                "--out",
                str(out),
                *extra,
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(argv)
            report = json.loads(out.read_text())
            results.append((f"{name}{suffix}", code, report))
    return results


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", choices=["mock", "llm"], default="mock")
    parser.add_argument("--only", help="只运行名称包含该字符串的用例")
    args = parser.parse_args()
    for name, runs in CASES:
        if args.only and args.only not in name:
            continue
        for label, code, rep in run_case(name, runs, args.policy):
            items = "; ".join(
                f"{i['material_id']}:{i['outcome']}"
                + (f"@{i['suggested_slot'][-5:]}" if i["suggested_slot"] else "")
                for i in rep["items"]
            )
            print(
                f"{label:28} exit={code} {rep['task_status']:18} "
                f"steps={rep['run']['steps']:<2} calls={rep['run']['tool_calls']:<2} {items}"
            )


if __name__ == "__main__":
    main_cli()
