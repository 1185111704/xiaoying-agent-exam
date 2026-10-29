"""替代时段不得早于要求（程序守卫）；finish 时仍有素材无结论则拒绝。"""

from src.agent import run_agent
from src.policies import ScriptedPolicy


def call(box, tool, **args):
    return box.call(1, tool, {**args, "reason": "r"})


M01 = {"material_id": "M01", "publish_at": "2026-11-03 18:00"}


def test_earlier_slot_rejected_without_window(make_box):
    box = make_box({"items": [M01]})
    call(box, "check_rules", material_id="M01")
    res = call(
        box, "save_draft", material_id="M01", slot="2026-11-03 17:00", needs_confirmation=True
    )
    assert res["status"] == "rejected" and res["error"] == "EARLIER_THAN_REQUESTED"
    assert (
        call(
            box, "save_draft", material_id="M01", slot="2026-11-03 19:00", needs_confirmation=True
        )["status"]
        == "saved"
    )


def test_window_can_allow_earlier(make_box):
    item = {**M01, "window": ["2026-11-03 16:00", "2026-11-03 21:00"]}
    box = make_box({"items": [item]})
    call(box, "check_rules", material_id="M01")
    assert (
        call(
            box, "save_draft", material_id="M01", slot="2026-11-03 17:00", needs_confirmation=True
        )["status"]
        == "saved"
    )


def test_finish_rejected_until_every_item_has_a_conclusion(make_box):
    request = {"items": [M01, {"material_id": "M02", "publish_at": "2026-11-03 10:00"}]}
    box = make_box(request)
    script = [
        [("block", {"material_id": "M01", "reason_code": "X", "reason": "r"})],
        [("finish", {"summary": "s", "reason": "r"})],
        [("request_confirmation", {"material_id": "M02", "question": "q", "reason": "r"})],
        [("finish", {"summary": "s2", "reason": "r"})],
    ]
    run = run_agent(ScriptedPolicy(script), box)
    first_finish = run.trace[1]["result"]
    assert first_finish["error"] == "UNRESOLVED_ITEMS" and first_finish["unresolved"] == ["M02"]
    assert run.status == "finished" and run.final_message == "s2"
