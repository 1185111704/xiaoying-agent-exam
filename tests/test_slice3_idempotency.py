"""切片 3：重复提交（幂等）、更换占用时段后重复提交（隐藏测试）、并发占位。"""

import json
import shutil
import sqlite3
import threading

import pytest

from src.config import Settings
from src.main import main
from src.store import Store

CONFLICT = "examples/04-conflict/input.json"
DUP = "examples/05-duplicate/input.json"


def run(capsys, *argv):
    code = main(list(map(str, argv)))
    return code, json.loads(capsys.readouterr().out)


def item(rep, mid):
    return next(i for i in rep["items"] if i["material_id"] == mid)


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    shutil.copytree(Settings().data_dir, d)
    return d


def edit_schedule(data, add=(), remove=()):
    path = data / "schedule.json"
    sched = [e for e in json.loads(path.read_text()) if e["material_id"] not in remove]
    sched += [
        {"account": "douyin_main", "slot": s, "material_id": m, "title": "新增"} for s, m in add
    ]
    path.write_text(json.dumps(sched, ensure_ascii=False))


# ---- 重复提交 ----
def test_same_request_twice_keeps_one_draft(tmp_path, capsys):
    db = tmp_path / "s.db"
    _, first = run(capsys, DUP, "--db", db)
    _, second = run(capsys, DUP, "--db", db)
    a, b = item(first, "M01"), item(second, "M01")
    assert a["draft"]["this_run"] == "saved" and b["draft"]["this_run"] == "unchanged"
    assert a["draft"]["draft_id"] == b["draft"]["draft_id"] and b["draft"]["version"] == 1
    assert Store(db).count_drafts() == 1


def test_already_scheduled_material_is_detected(tmp_path, capsys):
    _, rep = run(capsys, DUP, "--db", tmp_path / "s.db")
    m08 = item(rep, "M08")
    assert m08["outcome"] == "blocked" and m08["block"]["reason_code"] == "ALREADY_SCHEDULED"
    assert m08["draft"]["status"] == "not_saved"


def test_save_twice_in_one_run_is_unchanged(make_box):
    box = make_box({"items": [{"material_id": "M02", "publish_at": "2026-11-03 10:00"}]})
    box.call(1, "check_rules", {"material_id": "M02", "reason": "r"})
    args = {
        "material_id": "M02",
        "slot": "2026-11-03 10:00",
        "needs_confirmation": False,
        "reason": "r",
    }
    assert box.call(2, "save_draft", args)["status"] == "saved"
    assert box.call(3, "save_draft", args)["status"] == "unchanged"
    assert box.store.count_drafts() == 1


# ---- 隐藏测试：更换占用时段并重复提交 ----
def test_alternative_slot_taken_then_resubmit(tmp_path, data, capsys):
    """替代时段 19:00 被正式排期占用后重跑：同一份草稿换到下一个空位，版本 +1。"""
    db = tmp_path / "s.db"
    _, first = run(capsys, CONFLICT, "--db", db, "--data-dir", data)
    assert item(first, "M01")["suggested_slot"] == "2026-11-03 19:00"

    edit_schedule(data, add=[("2026-11-03 19:00", "X40")])
    _, second = run(capsys, CONFLICT, "--db", db, "--data-dir", data)
    m01 = item(second, "M01")
    assert m01["suggested_slot"] == "2026-11-03 21:00"
    assert m01["draft"]["this_run"] == "updated" and m01["draft"]["version"] == 2
    assert m01["draft"]["draft_id"] == item(first, "M01")["draft"]["draft_id"]
    assert m01["notices"] and "19:00" in m01["notices"][0]
    assert m01["pending_confirmations"]
    assert Store(db).count_drafts() == 1


def test_requested_slot_freed_then_resubmit(tmp_path, data, capsys):
    """原要求 18:00 被释放后重跑：草稿回到 18:00、不再待确认，19:00 被释放。"""
    db = tmp_path / "s.db"
    run(capsys, CONFLICT, "--db", db, "--data-dir", data)
    edit_schedule(data, remove=["X21"])
    code, rep = run(capsys, CONFLICT, "--db", db, "--data-dir", data)
    m01, m07 = item(rep, "M01"), item(rep, "M07")
    assert m01["suggested_slot"] == "2026-11-03 18:00" and not m01["pending_confirmations"]
    assert m01["draft"]["previous"]["slot"] == "2026-11-03 19:00"
    assert m01["outcome"] == "draft_saved"
    # M07 也要 18:00（硬性）：M01 先占了，M07 仍需确认，不能擅自改
    assert m07["outcome"] == "awaiting_confirmation"
    assert Store(db).occupancy("douyin_main", "2026-11-03").get("2026-11-03 19:00") is None


# ---- 并发 ----
def test_conflict_keeps_own_previous_hold(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save_draft("M01", "douyin_main", "2026-11-03 19:00", "pending_confirmation")
    store.save_draft("M02", "douyin_main", "2026-11-03 10:00", "draft")
    res = store.save_draft("M02", "douyin_main", "2026-11-03 19:00", "pending_confirmation")
    assert res["status"] == "conflict" and res["holder"]["material_id"] == "M01"
    occ = store.occupancy("douyin_main", "2026-11-03")
    assert occ["2026-11-03 10:00"]["material_id"] == "M02"  # 回滚：旧占位仍在


def test_ten_threads_race_for_one_slot(tmp_path):
    db = tmp_path / "s.db"
    Store(db)
    results, barrier = [], threading.Barrier(10)

    def worker(i):
        store = Store(db)
        barrier.wait()
        results.append(store.save_draft(f"T{i}", "douyin_main", "2026-11-03 19:00", "draft"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r["status"] for r in results)
    assert statuses.count("saved") == 1 and statuses.count("conflict") == 9
    assert len(Store(db).occupancy("douyin_main", "2026-11-03")) == 1


def test_lock_timeout_reports_failed_not_success(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db, busy_timeout=0.2)
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        res = store.save_draft("M02", "douyin_main", "2026-11-03 10:00", "draft")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert res["ok"] is False and res["status"] == "failed" and res["error"] == "STORAGE_ERROR"
    assert store.count_drafts() == 0
