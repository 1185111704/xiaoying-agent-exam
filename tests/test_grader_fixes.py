"""冷启动阅卷评审发现的问题的回归测试。"""

import json
import shutil

from src.agent import run_agent
from src.config import Settings
from src.policies import ScriptedPolicy
from src.report import build_report
from src.store import Store
from src.tools import ToolBox

REQ = {"request_id": "T", "items": [{"material_id": "M02", "publish_at": "2026-11-03 10:00"}]}


def _data(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(Settings().data_dir, data)
    return data


def _make_caption_risky(data):
    path = data / "materials.json"
    m = json.loads(path.read_text())
    m["M02"]["caption"] = "全网最强调色教程"
    path.write_text(json.dumps(m, ensure_ascii=False))


def _first_run(data, db):
    box = ToolBox(REQ, data, Store(db))
    box.call(1, "check_rules", {"material_id": "M02", "reason": "r"})
    box.call(
        2,
        "save_draft",
        {
            "material_id": "M02",
            "slot": "2026-11-03 10:00",
            "needs_confirmation": False,
            "reason": "r",
        },
    )


def test_stale_draft_from_previous_run_is_not_a_conclusion(tmp_path):
    """上一轮的草稿不能代替本轮审核：素材变得违规后，直接 finish 被拒，不会报"已完成"。"""
    data, db = _data(tmp_path), tmp_path / "s.db"
    _first_run(data, db)
    _make_caption_risky(data)

    box = ToolBox(REQ, data, Store(db))
    run = run_agent(
        ScriptedPolicy([[("finish", {"summary": "s", "reason": "r"})]]), box, max_steps=3
    )
    assert run.trace[0]["result"]["error"] == "UNRESOLVED_ITEMS"
    rep = build_report(REQ, box, run)
    item = rep["items"][0]
    assert rep["task_status"] == "incomplete" and item["outcome"] == "unresolved"
    assert item["suggested_slot"] is None
    assert item["draft"]["status"] == "draft"  # 草稿事实如实展示
    assert any("上一轮" in n for n in item["notices"])


def test_stale_draft_resave_requires_fresh_review(tmp_path):
    """本轮想沿用旧草稿必须重新过守卫：素材已违规时保存被拒，只能阻断。"""
    data, db = _data(tmp_path), tmp_path / "s.db"
    _first_run(data, db)
    _make_caption_risky(data)

    box = ToolBox(REQ, data, Store(db))
    save = {
        "material_id": "M02",
        "slot": "2026-11-03 10:00",
        "needs_confirmation": False,
        "reason": "r",
    }
    assert box.call(1, "save_draft", save)["error"] == "NOT_CHECKED"
    box.call(2, "check_rules", {"material_id": "M02", "reason": "r"})
    assert box.call(3, "save_draft", save)["error"] == "RULE_BLOCKED"
    box.call(4, "block", {"material_id": "M02", "reason_code": "AD-01", "reason": "r"})
    assert box.store.draft_state("M02")["status"] == "cancelled"
