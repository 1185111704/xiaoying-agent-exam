"""切片 1：读素材（缺失分档、已排期识别）、查时段（正式排期/草稿占位）、保存草稿（落库占位）。"""

import json

from src.config import Settings
from src.store import Store
from src.tools import ToolBox


def req(*mids, at="2026-11-03 10:00"):
    return {"request_id": "T", "items": [{"material_id": m, "publish_at": at} for m in mids]}


def call(box, tool, **args):
    return box.call(1, tool, {**args, "reason": "test"})


def test_read_material_missing_tiers(make_box):
    box = make_box(req("M03", "M05", "M06", "M02"))
    m03 = call(box, "read_material", material_id="M03")["missing"]
    assert set(m03["reject"]) == {"caption"} and set(m03["todo"]) == {"cover", "tags"}
    assert set(call(box, "read_material", material_id="M05")["missing"]["lookup"]) == {
        "people.portrait_release_id"
    }
    assert set(call(box, "read_material", material_id="M06")["missing"]["lookup"]) == {
        "music.license_id"
    }
    assert call(box, "read_material", material_id="M02")["missing"] == {}


def test_read_material_flags_already_scheduled(make_box):
    res = call(make_box(req("M08")), "read_material", material_id="M08")
    assert res["already_scheduled"][0]["slot"] == "2026-11-03 12:00"


def test_read_material_outside_request_or_library(make_box):
    assert (
        call(make_box(req("M02")), "read_material", material_id="M01")["error"] == "NOT_IN_REQUEST"
    )
    assert call(make_box(req("M99")), "read_material", material_id="M99")["error"] == (
        "MATERIAL_NOT_FOUND"
    )


def _slots(res):
    return {s["slot"][-5:]: s for s in res["slots"]}


def test_query_slots_shows_schedule_and_holds(make_box):
    box = make_box(req("M01", "M02"))
    s = _slots(
        call(box, "query_slots", account="douyin_main", date="2026-11-03", material_id="M01")
    )
    assert s["18:00"]["status"] == "scheduled" and s["18:00"]["holder"] == "X21"
    assert s["19:00"]["status"] == "free" and len(s) == 14  # 09:00–22:00

    assert (
        call(
            box, "save_draft", material_id="M02", slot="2026-11-03 19:00", needs_confirmation=True
        )["status"]
        == "saved"
    )
    other = _slots(
        call(box, "query_slots", account="douyin_main", date="2026-11-03", material_id="M01")
    )
    own = _slots(
        call(box, "query_slots", account="douyin_main", date="2026-11-03", material_id="M02")
    )
    assert other["19:00"]["status"] == "held" and other["19:00"]["holder"] == "M02"
    assert own["19:00"]["status"] == "free"  # 自己的占位对自己视为空闲


def test_save_draft_persists_across_runs(make_box):
    first = make_box(req("M02"))
    assert (
        call(
            first,
            "save_draft",
            material_id="M02",
            slot="2026-11-03 10:00",
            needs_confirmation=False,
        )["status"]
        == "saved"
    )
    second = make_box(req("M01"))  # 新一次运行，同一个状态库
    s = _slots(
        call(second, "query_slots", account="douyin_main", date="2026-11-03", material_id="M01")
    )
    assert s["10:00"]["status"] == "held"


def test_save_draft_on_scheduled_slot_is_conflict(make_box):
    res = call(
        make_box(req("M01")),
        "save_draft",
        material_id="M01",
        slot="2026-11-03 18:00",
        needs_confirmation=False,
    )
    assert (
        res["ok"] is False and res["status"] == "conflict" and res["holder"]["material_id"] == "X21"
    )


def test_save_draft_rejects_bad_slot(make_box):
    box = make_box(req("M02"))
    for slot in ["2026-11-03 10:30", "2026-11-03 23:00", "明天上午"]:
        res = call(box, "save_draft", material_id="M02", slot=slot, needs_confirmation=False)
        assert res["status"] == "rejected" and res["error"] == "INVALID_SLOT"


def test_schedule_sync_displaces_draft(tmp_path):
    """正式排期新增了草稿所占的时段：草稿被标记 displaced，正式排期优先。"""
    data = tmp_path / "data"
    data.mkdir()
    for f in Settings().data_dir.iterdir():
        (data / f.name).write_text(f.read_text())
    store = Store(tmp_path / "s.db")
    box = ToolBox(req("M02"), data, store)
    call(box, "save_draft", material_id="M02", slot="2026-11-03 10:00", needs_confirmation=False)

    sched = json.loads((data / "schedule.json").read_text())
    sched.append({"account": "douyin_main", "slot": "2026-11-03 10:00", "material_id": "X30"})
    (data / "schedule.json").write_text(json.dumps(sched))
    box2 = ToolBox(req("M02"), data, store)
    assert box2.displaced and box2.displaced[0]["material_id"] == "M02"
    assert store.get_draft("M02", "douyin_main")["status"] == "displaced"
