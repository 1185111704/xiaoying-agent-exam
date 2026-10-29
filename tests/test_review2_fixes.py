"""第二次代码审查（保存层）修复的回归测试。"""

import json

import pytest

from src.agent import RunResult, run_agent
from src.main import load_request
from src.policies import ScriptedPolicy
from src.report import build_report
from src.store import Store


def req(*items):
    return {"request_id": "T", "items": list(items)}


def call(box, tool, **args):
    return box.call(1, tool, {**args, "reason": "r"})


M01 = {"material_id": "M01", "publish_at": "2026-11-03 18:00"}
M02 = {"material_id": "M02", "publish_at": "2026-11-03 10:00"}


def test_non_padded_slot_cannot_bypass_conflict(make_box):
    """②：不补零的写法被拒，不能绕过 (account, slot) 主键二次占用 18:00。"""
    box = make_box(req(M01))
    call(box, "check_rules", material_id="M01")
    res = call(
        box, "save_draft", material_id="M01", slot="2026-11-3 18:00", needs_confirmation=True
    )
    assert res["status"] == "rejected" and res["error"] == "INVALID_SLOT"
    q = call(box, "query_slots", account="douyin_main", date="2026-11-3", material_id="M01")
    assert q["error"] == "INVALID_ARGS"


def test_failed_calls_do_not_satisfy_guards(make_box):
    """③：参数错误的 block / check_rules 不算数。"""
    box = make_box(req(M02))
    box.call(1, "check_rules", {"material_id": "M02"})  # 缺 reason → INVALID_ARGS
    res = call(
        box, "save_draft", material_id="M02", slot="2026-11-03 10:00", needs_confirmation=False
    )
    assert res["error"] == "NOT_CHECKED"
    box.call(1, "block", {"material_id": "M02"})  # 缺 reason_code → INVALID_ARGS
    call(box, "check_rules", material_id="M02")
    assert (
        call(
            box, "save_draft", material_id="M02", slot="2026-11-03 10:00", needs_confirmation=False
        )["status"]
        == "saved"
    )


def test_account_change_updates_same_draft(tmp_path):
    """⑤：素材换账号后保存，同一份草稿随之更新，旧账号的占位被释放。"""
    store = Store(tmp_path / "s.db")
    store.save_draft("M02", "douyin_main", "2026-11-03 10:00", "draft")
    res = store.save_draft("M02", "xhs_main", "2026-11-03 10:00", "draft")
    assert res["status"] == "updated" and res["draft_id"] == "D-M02" and res["version"] == 2
    assert store.occupancy("douyin_main", "2026-11-03") == {}
    assert store.count_drafts() == 1


def test_cancel_displaced_draft_releases_nothing(tmp_path):
    """⑥：草稿已被正式排期挤掉，撤销时不声称释放了时段。"""
    store = Store(tmp_path / "s.db")
    store.save_draft("M02", "douyin_main", "2026-11-03 10:00", "draft")
    store.sync_schedule(
        [{"account": "douyin_main", "slot": "2026-11-03 10:00", "material_id": "X50"}]
    )
    res = store.cancel_draft("M02", "douyin_main")
    assert res["released_slot"] is None


def test_finish_not_last_is_rejected_and_rest_runs(make_box):
    """⑦：同一轮 [finish, check_rules]：finish 被拒，check_rules 照常执行，运行继续。"""
    request = req(M02)
    box = make_box(request)
    script = [
        [
            ("finish", {"summary": "s", "reason": "r"}),
            ("check_rules", {"material_id": "M02", "reason": "r"}),
        ],
        [("finish", {"summary": "s2", "reason": "r"})],
    ]
    run = run_agent(ScriptedPolicy(script), box)
    assert run.status == "finished" and run.final_message == "s2"
    assert run.trace[0]["result"]["error"] == "FINISH_NOT_LAST"
    assert run.trace[1]["tool"] == "check_rules" and run.trace[1]["result"]["ok"]


def test_rollback_after_sqlite_auto_rollback_keeps_original_error(tmp_path):
    """⑧：事务已被 SQLite 自行结束时，不再 ROLLBACK 掩盖原始错误。"""
    store = Store(tmp_path / "s.db")
    with pytest.raises(ZeroDivisionError):
        with store._tx() as conn:
            conn.execute("ROLLBACK")  # 模拟 SQLite 自动回滚
            raise ZeroDivisionError("原始错误")


def _report_with_summary(make_box, summary, failed=("M02",), saved=("M01",)):
    request = req(M01, M02)
    box = make_box(request)
    from src.tools import LedgerEntry

    for mid in saved:
        box.store.save_draft(mid, "douyin_main", "2026-11-03 18:00", "draft")
        box.ledger.entries.append(
            LedgerEntry(
                1,
                "save_draft",
                {"material_id": mid, "slot": "2026-11-03 18:00", "needs_confirmation": False},
                "r",
                {"ok": True, "status": "saved", "draft_id": f"D-{mid}", "version": 1},
            )
        )
    for mid in failed:
        box.ledger.entries.append(
            LedgerEntry(
                1,
                "save_draft",
                {"material_id": mid, "slot": "2026-11-03 10:00", "needs_confirmation": False},
                "r",
                {"ok": False, "status": "failed"},
            )
        )
    run = RunResult("finished", 1, 1, final_message=summary)
    return build_report(request, box, run)["consistency_warnings"]


def test_correct_multi_item_summary_not_flagged(make_box):
    """⑨：多素材时如实的总结不误报。"""
    assert _report_with_summary(make_box, "M01 已保存；M02 保存失败") == []


@pytest.mark.parametrize("summary", ["M02 已保存", "全部已保存成功"])
def test_false_success_still_flagged(make_box, summary):
    assert _report_with_summary(make_box, summary)


@pytest.mark.parametrize(
    "items",
    [
        [M01, M01],
        [{**M01, "publish_at": "2026-11-3 18:00"}],
        [{**M01, "fixed": "yes"}],
        [{**M01, "window": "17:00-19:00"}],
        [{**M01, "window": ["2026-11-03 19:00", "2026-11-03 17:00"]}],
    ],
)
def test_request_validation(tmp_path, items):
    """⑩：重复素材、非标准时间、fixed/window 类型错误在输入阶段拒绝。"""
    path = tmp_path / "in.json"
    path.write_text(json.dumps({"items": items}))
    with pytest.raises(ValueError):
        load_request(path)


def test_report_reads_draft_from_database_across_runs(make_box):
    """④：上一次运行存的草稿仍在库中；本次模型只请求确认，报告仍如实显示草稿与占位。"""
    request = req(M01)
    first = make_box(request)
    call(first, "check_rules", material_id="M01")
    call(first, "save_draft", material_id="M01", slot="2026-11-03 19:00", needs_confirmation=True)

    second = make_box(request)  # 同一状态库的新一次运行
    script = [
        [
            (
                "request_confirmation",
                {"material_id": "M01", "question": "是否接受 19:00？", "reason": "r"},
            )
        ],
        [("finish", {"summary": "s", "reason": "r"})],
    ]
    rep = build_report(request, second, run_agent(ScriptedPolicy(script), second))
    m01 = rep["items"][0]
    assert m01["draft"]["status"] == "pending_confirmation"
    assert m01["draft"]["slot"] == "2026-11-03 19:00" and m01["draft"]["this_run"] == "none"
    assert m01["suggested_slot"] == "2026-11-03 19:00"
    assert m01["outcome"] == "draft_saved_pending_confirmation"
