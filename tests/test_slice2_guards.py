"""切片 2：保存守卫（确认边界、硬性要求、窗口、审核前置）与报告按事实判定待确认。"""

import json

import pytest

from src.agent import RunResult
from src.main import main
from src.report import build_report
from src.tools import LedgerEntry


def req(items):
    return {"request_id": "T", "items": items}


def call(box, tool, **args):
    return box.call(1, tool, {**args, "reason": "test"})


def save(box, mid, slot, flag):
    return call(box, "save_draft", material_id=mid, slot=slot, needs_confirmation=flag)


M01_18 = {"material_id": "M01", "publish_at": "2026-11-03 18:00"}


def test_changed_slot_requires_confirmation_flag(make_box):
    box = make_box(req([M01_18]))
    call(box, "check_rules", material_id="M01")
    res = save(box, "M01", "2026-11-03 19:00", False)
    assert res["status"] == "rejected" and res["error"] == "CONFIRMATION_REQUIRED"
    assert save(box, "M01", "2026-11-03 19:00", True)["status"] == "saved"


def test_flag_must_match_fact(make_box):
    box = make_box(req([{"material_id": "M02", "publish_at": "2026-11-03 10:00"}]))
    call(box, "check_rules", material_id="M02")
    assert save(box, "M02", "2026-11-03 10:00", True)["error"] == "CONFIRMATION_MISMATCH"


def test_fixed_requirement_cannot_be_moved(make_box):
    box = make_box(req([{"material_id": "M07", "publish_at": "2026-11-03 18:00", "fixed": True}]))
    call(box, "check_rules", material_id="M07")
    res = save(box, "M07", "2026-11-03 19:00", True)
    assert res["status"] == "rejected" and res["error"] == "FIXED_REQUIREMENT"


def test_window_is_hard(make_box):
    item = {**M01_18, "window": ["2026-11-03 17:00", "2026-11-03 19:00"]}
    box = make_box(req([item]))
    call(box, "check_rules", material_id="M01")
    assert save(box, "M01", "2026-11-03 21:00", True)["error"] == "OUTSIDE_WINDOW"
    assert save(box, "M01", "2026-11-03 19:00", True)["status"] == "saved"


@pytest.mark.parametrize(
    "mid, prep, error",
    [
        ("M02", [], "NOT_CHECKED"),
        ("M04", ["check_rules"], "RULE_BLOCKED"),
        ("M03", ["check_rules"], "FIELD_MISSING"),
        ("M06", ["check_rules"], "LICENSE_UNVERIFIED"),
        ("M08", ["check_rules"], "ALREADY_SCHEDULED"),
        ("M02", ["check_rules", "block"], "BLOCKED"),
    ],
)
def test_review_must_pass_before_save(make_box, mid, prep, error):
    box = make_box(req([{"material_id": mid, "publish_at": "2026-11-03 10:00"}]))
    for tool in prep:
        extra = {"reason_code": "X"} if tool == "block" else {}
        call(box, tool, material_id=mid, **extra)
    res = save(box, mid, "2026-11-03 10:00", False)
    assert res["status"] == "rejected" and res["error"] == error


def test_check_rules_returns_evidence(make_box):
    box = make_box(req([{"material_id": "M04", "publish_at": "2026-11-03 10:00"}]))
    hits = call(box, "check_rules", material_id="M04")["hits"]
    assert {h["matched"] for h in hits} >= {"最强", "第一"}
    assert all(h["severity"] == "block" and h["matched"] in h["evidence"] for h in hits)


def test_report_ignores_model_flag(make_box):
    """骨架审查 #3：即使记录里模型声称无需确认，只要时段改了，报告就列为待确认。"""
    request = req([M01_18])
    box = make_box(request)
    box.ledger.entries.append(
        LedgerEntry(
            1,
            "save_draft",
            {"material_id": "M01", "slot": "2026-11-03 19:00", "needs_confirmation": False},
            "r",
            {"ok": True, "status": "saved", "draft_id": "D-M01", "version": 1},
        )
    )
    rep = build_report(request, box.ledger, RunResult("finished", 1, 1))
    assert rep["items"][0]["outcome"] == "draft_saved_pending_confirmation"
    assert rep["task_status"] == "needs_confirmation"


def test_reference_instance_end_to_end(tmp_path, capsys):
    """参考实例（离线 Mock 走流程）：M01 改 19:00 待确认；M07 硬性要求不动、请求确认。"""
    code = main(["examples/04-conflict/input.json", "--db", str(tmp_path / "s.db")])
    rep = json.loads(capsys.readouterr().out)
    m01, m07 = rep["items"]
    assert code == 2 and rep["task_status"] == "needs_confirmation"
    assert m01["suggested_slot"] == "2026-11-03 19:00" and m01["pending_confirmations"]
    assert m01["publish_status"] == "not_published"
    assert m07["outcome"] == "awaiting_confirmation" and m07["suggested_slot"] is None
    assert m07["draft"]["status"] == "not_saved"
