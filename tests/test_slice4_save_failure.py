"""切片 4：保存失败——如实返回失败、事务回滚、是否重试由模型决定。"""

import json

from src.agent import run_agent
from src.main import main
from src.policies import ScriptedPolicy
from src.report import build_report
from src.store import Store

FAIL = "examples/06-save-failure/input.json"


def test_persistent_failure_is_reported_truthfully(tmp_path, capsys):
    db = tmp_path / "s.db"
    code = main([FAIL, "--db", str(db), "--fault-save", "always"])
    rep = json.loads(capsys.readouterr().out)
    m02 = rep["items"][0]
    assert code == 1 and rep["task_status"] == "save_failed"
    assert m02["outcome"] == "save_failed" and m02["draft"]["status"] == "failed"
    assert m02["suggested_slot"] is None and m02["draft"]["draft_id"] is None
    assert Store(db).count_drafts() == 0  # 回滚：没有半成品
    assert Store(db).occupancy("douyin_main", "2026-11-03").get("2026-11-03 10:00") is None


def test_failed_update_keeps_previous_draft(tmp_path):
    db = tmp_path / "s.db"
    Store(db).save_draft("M01", "douyin_main", "2026-11-03 19:00", "pending_confirmation")
    res = Store(db, fault="always").save_draft(
        "M01", "douyin_main", "2026-11-03 21:00", "pending_confirmation"
    )
    assert res["status"] == "failed"
    draft = Store(db).get_draft("M01", "douyin_main")
    assert draft["slot"] == "2026-11-03 19:00" and draft["version"] == 1
    assert "2026-11-03 19:00" in Store(db).occupancy("douyin_main", "2026-11-03")


REQ = {"request_id": "T", "items": [{"material_id": "M02", "publish_at": "2026-11-03 10:00"}]}
SAVE = (
    "save_draft",
    {"material_id": "M02", "slot": "2026-11-03 10:00", "needs_confirmation": False, "reason": "r"},
)


def _run(tmp_path, fault, script):
    from src.config import Settings
    from src.tools import ToolBox

    box = ToolBox(REQ, Settings().data_dir, Store(tmp_path / "s.db", fault=fault))
    run = run_agent(ScriptedPolicy(script), box)
    return build_report(REQ, box.ledger, run)


def test_retry_is_a_model_decision(tmp_path):
    """偶发失败：程序不自动重试；模型选择再保存一次后成功，报告以最终结果为准。"""
    check = ("check_rules", {"material_id": "M02", "reason": "r"})
    rep = _run(
        tmp_path, "once", [[check], [SAVE], [SAVE], [("finish", {"summary": "s", "reason": "r"})]]
    )
    saves = [t for t in rep["trace"] if t["tool"] == "save_draft"]
    assert [s["result"]["status"] for s in saves] == ["failed", "saved"]
    assert rep["items"][0]["outcome"] == "draft_saved" and rep["task_status"] == "completed"


def test_summary_claiming_success_is_flagged(tmp_path):
    check = ("check_rules", {"material_id": "M02", "reason": "r"})
    rep = _run(
        tmp_path,
        "always",
        [[check], [SAVE], [("finish", {"summary": "M02 已保存成功", "reason": "r"})]],
    )
    assert rep["task_status"] == "save_failed"
    assert any("保存成功" in w for w in rep["consistency_warnings"])
