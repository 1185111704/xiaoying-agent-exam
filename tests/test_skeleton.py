"""骨架链路测试：脚本化假模型走完一遍循环，验证报告结构、步数上限与 trace。"""

from src.agent import run_agent
from src.config import Settings
from src.policies import ScriptedPolicy
from src.report import build_report
from src.tools import ToolBox

REQ = {"request_id": "T", "items": [{"material_id": "M02", "publish_at": "2026-11-03 10:00"}]}


def _run(script, **limits):
    box = ToolBox(REQ, Settings().data_dir)
    run = run_agent(ScriptedPolicy(script), box, **limits)
    return build_report(REQ, box.ledger, run)


def r(**kw):
    return {**kw, "reason": "test"}


def test_happy_path_chain():
    rep = _run(
        [
            [("read_material", r(material_id="M02"))],
            [("check_rules", r(material_id="M02"))],
            [("query_slots", r(account="douyin_main", date="2026-11-03", material_id="M02"))],
            [
                (
                    "save_draft",
                    r(material_id="M02", slot="2026-11-03 10:00", needs_confirmation=False),
                )
            ],
            [("finish", r(summary="完成"))],
        ]
    )
    assert rep["task_status"] == "completed"
    item = rep["items"][0]
    assert item["outcome"] == "draft_saved"
    assert item["publish_status"] == "not_published"
    assert [t["tool"] for t in rep["trace"]][-1] == "finish"
    assert rep["trace"][0]["next_action"] == "check_rules"


def test_step_limit_returns_incomplete():
    rep = _run([[("read_material", r(material_id="M02"))]] * 10, max_steps=3)
    assert rep["run"]["status"] == "step_limit"
    assert rep["task_status"] == "incomplete"


def test_invalid_args_returned_as_observation():
    rep = _run([[("save_draft", {"material_id": "M02"})], [("finish", r(summary="x"))]])
    assert "INVALID_ARGS" in rep["trace"][0]["result"]
