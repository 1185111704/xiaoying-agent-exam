"""模型输出不规范、输入文件不规范时不崩溃，并给出明确结果。"""

import json

import pytest

from src.agent import run_agent
from src.config import load_dotenv
from src.main import load_request, main
from src.policies import ScriptedPolicy
from src.report import build_report

REQ = {"request_id": "T", "items": [{"material_id": "M02", "publish_at": "2026-11-03 10:00"}]}


@pytest.mark.parametrize(
    "args, detail",
    [
        ({"material_id": "M02", "account": "x", "reason": "r"}, "未知参数"),
        (None, "JSON 对象"),
        ([1, 2], "JSON 对象"),
        ({"material_id": 2, "reason": "r"}, "类型"),
        ({"material_id": "M02"}, "缺少参数"),
    ],
)
def test_bad_model_args_become_invalid_args(make_box, args, detail):
    result = make_box(REQ).call(1, "read_material", args)
    assert result["error"] == "INVALID_ARGS" and detail in result["detail"]


def test_string_boolean_rejected(make_box):
    args = {
        "material_id": "M02",
        "slot": "2026-11-03 10:00",
        "needs_confirmation": "false",
        "reason": "r",
    }
    assert make_box(REQ).call(1, "save_draft", args)["error"] == "INVALID_ARGS"


def test_tool_exception_is_observation_not_crash(make_box, monkeypatch):
    box = make_box(REQ)
    monkeypatch.setattr(box, "_check_rules", lambda **_: 1 / 0)
    result = box.call(1, "check_rules", {"material_id": "M02", "reason": "r"})
    assert result["ok"] is False and result["error"] == "TOOL_ERROR"


def test_tool_limit_stops_before_next_model_call(make_box):
    calls = []

    class Counting(ScriptedPolicy):
        def decide(self, messages, tools):
            calls.append(1)
            return super().decide(messages, tools)

    script = [[("read_material", {"material_id": "M02", "reason": "r"})]] * 5
    box = make_box(REQ)
    run = run_agent(Counting(script), box, max_steps=10, max_tool_calls=2)
    assert run.status == "tool_limit" and len(calls) == 2
    assert build_report(REQ, box.ledger, run)["task_status"] == "incomplete"


def test_published_claim_always_flagged(make_box):
    box = make_box(REQ)
    run = run_agent(
        ScriptedPolicy([[("finish", {"summary": "M01 未发布；M02 已发布", "reason": "r"})]]), box
    )
    assert build_report(REQ, box.ledger, run)["consistency_warnings"]


@pytest.mark.parametrize("payload", [[1], {"items": ["M02"]}, {"items": []}])
def test_malformed_input_file(tmp_path, payload):
    path = tmp_path / "in.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_request(path)
    assert main([str(path)]) == 1


def test_non_positive_limit_rejected(tmp_path):
    with pytest.raises(SystemExit):
        main(["x.json", "--max-tool-calls", "0"])


def test_dotenv_quotes_and_export(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("export T_KEY_A=\"sk-abc\"\nT_KEY_B='x'\n")
    monkeypatch.delenv("T_KEY_A", raising=False)
    monkeypatch.delenv("T_KEY_B", raising=False)
    load_dotenv(env)
    import os

    assert os.environ["T_KEY_A"] == "sk-abc" and os.environ["T_KEY_B"] == "x"
