"""切片 5：授权补查与规则风险（信息缺失、规则风险两个验收点）。"""

import json

from src.main import main


def call(box, tool, **args):
    return box.call(1, tool, {**args, "reason": "r"})


def req(mid, at="2026-11-04 21:00"):
    return {"request_id": "T", "items": [{"material_id": mid, "publish_at": at}]}


def test_lookup_found_unlocks_save(make_box):
    box = make_box(req("M06"))
    call(box, "check_rules", material_id="M06")
    blocked = call(
        box, "save_draft", material_id="M06", slot="2026-11-04 21:00", needs_confirmation=False
    )
    assert blocked["error"] == "LICENSE_UNVERIFIED"
    found = call(box, "lookup_license", material_id="M06", kind="music")
    assert found["status"] == "found" and found["record"]["license_id"] == "LIC-0606"
    assert (
        call(
            box, "save_draft", material_id="M06", slot="2026-11-04 21:00", needs_confirmation=False
        )["status"]
        == "saved"
    )


def test_lookup_not_found_and_expired(make_box):
    assert (
        call(make_box(req("M05")), "lookup_license", material_id="M05", kind="portrait")["status"]
        == "not_found"
    )
    # M01 的音乐授权有效期到 2027-06-30：要求 2027-07-01 发布即视为过期
    late = call(
        make_box(req("M01", "2027-07-01 10:00")), "lookup_license", material_id="M01", kind="music"
    )
    assert late["status"] == "expired"
    assert (
        call(make_box(req("M02")), "lookup_license", material_id="M01", kind="music")["error"]
        == "NOT_IN_REQUEST"
    )


def _run(capsys, tmp_path, example):
    code = main([f"examples/{example}/input.json", "--db", str(tmp_path / "s.db")])
    rep = json.loads(capsys.readouterr().out)
    return code, {i["material_id"]: i for i in rep["items"]}, rep


def test_missing_info_example(tmp_path, capsys):
    code, items, rep = _run(capsys, tmp_path, "02-missing-info")
    assert items["M03"]["outcome"] == "blocked" and items["M03"]["review_status"] == "info_missing"
    assert items["M05"]["outcome"] == "awaiting_confirmation"
    assert items["M05"]["review_status"] == "info_missing"
    assert items["M06"]["outcome"] == "draft_saved" and items["M06"]["review_status"] == "passed"
    assert items["M99"]["outcome"] == "blocked"
    assert any(r["code"] == "MATERIAL_NOT_FOUND" for r in items["M99"]["risk_reasons"])
    tools = [t["tool"] for t in rep["trace"] if t["args"].get("material_id") == "M06"]
    assert "lookup_license" in tools  # 补查后才排期
    assert code == 2


def test_rule_risk_example(tmp_path, capsys):
    code, items, _ = _run(capsys, tmp_path, "03-rule-risk")
    m04 = items["M04"]
    assert m04["outcome"] == "blocked" and m04["review_status"] == "blocked"
    assert m04["block"]["reason_code"] == "AD-01" and m04["suggested_slot"] is None
    assert {r["evidence"] for r in m04["risk_reasons"]} and all(
        r["code"] == "AD-01" for r in m04["risk_reasons"]
    )
    assert items["M02"]["outcome"] == "draft_saved"
    assert code == 0
