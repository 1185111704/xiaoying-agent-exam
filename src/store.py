"""存储层：SQLite 中的时段占位与排期草稿。

- 占位表以 (account, slot) 为主键：同一账号同一时段在数据库层面只能有一条，与进程数无关。
- 正式排期（data/schedule.json）每次启动同步进占位表，优先于草稿。
- 每次保存是一个 BEGIN IMMEDIATE 事务：先拿写锁，再"释放旧占位 → 占新时段 → 写草稿"，
  任何一步失败整体回滚。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    account     TEXT NOT NULL,
    slot        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('scheduled', 'draft')),
    material_id TEXT NOT NULL,
    draft_id    TEXT,
    title       TEXT,
    PRIMARY KEY (account, slot)
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id    TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    account     TEXT NOT NULL,
    slot        TEXT,
    status      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    request_id  TEXT,
    note        TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (material_id, account)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path | str, busy_timeout: float = 5.0, fault: str | None = None):
        self.db_path = str(db_path)
        self.busy_timeout = busy_timeout
        # 故障注入（只用于验收"保存失败"）：always=每次保存都在提交前失败；once=仅第一次失败
        if fault not in (None, "always", "once"):
            raise ValueError(f"未知的故障模式 {fault}")
        self.fault = fault
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # 每次操作独立连接：便于多线程/多进程并发访问；autocommit 模式下手动管理事务
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    # ---- 正式排期同步 ----
    def sync_schedule(self, schedule: list[dict]) -> list[dict]:
        """用正式排期覆盖占位表中的 scheduled 行。被正式排期挤掉的草稿标记为 displaced 并返回。"""
        displaced = []
        with self._tx() as conn:
            conn.execute("DELETE FROM slots WHERE kind = 'scheduled'")
            for e in schedule:
                row = conn.execute(
                    "SELECT * FROM slots WHERE account = ? AND slot = ?", (e["account"], e["slot"])
                ).fetchone()
                if row is not None:  # 只可能是草稿占位：正式排期优先
                    conn.execute(
                        "DELETE FROM slots WHERE account = ? AND slot = ?",
                        (e["account"], e["slot"]),
                    )
                    conn.execute(
                        "UPDATE drafts SET status = 'displaced', updated_at = ? WHERE draft_id = ?",
                        (_now(), row["draft_id"]),
                    )
                    displaced.append(dict(row))
                conn.execute(
                    "INSERT INTO slots (account, slot, kind, material_id, title) "
                    "VALUES (?, ?, 'scheduled', ?, ?)",
                    (e["account"], e["slot"], e["material_id"], e.get("title")),
                )
        return displaced

    # ---- 查询 ----
    def occupancy(self, account: str, date: str) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM slots WHERE account = ? AND slot LIKE ?", (account, f"{date} %")
            ).fetchall()
        return {r["slot"]: dict(r) for r in rows}

    def get_draft(self, material_id: str, account: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM drafts WHERE material_id = ? AND account = ?", (material_id, account)
            ).fetchone()
        return dict(row) if row else None

    def draft_state(self, material_id: str) -> dict | None:
        """素材草稿在库中的最终状态，以及它是否仍占着时段。报告以此为准。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT draft_id, account, slot, status, version FROM drafts WHERE material_id = ?",
                (material_id,),
            ).fetchone()
            if row is None:
                return None
            held = conn.execute(
                "SELECT 1 FROM slots WHERE kind = 'draft' AND material_id = ? AND slot = ?",
                (material_id, row["slot"]),
            ).fetchone()
        return {**dict(row), "holds_slot": held is not None}

    def count_drafts(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]

    # ---- 撤销 ----
    def cancel_draft(self, material_id: str, account: str) -> dict | None:
        """撤销素材的活动草稿并释放时段（同一事务）。没有活动草稿时返回 None。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM drafts WHERE material_id = ? AND status != 'cancelled'",
                (material_id,),
            ).fetchone()
            if row is None:
                return None
            released = conn.execute(
                "DELETE FROM slots WHERE kind = 'draft' AND material_id = ?", (material_id,)
            ).rowcount
            conn.execute(
                "UPDATE drafts SET status = 'cancelled', version = version + 1, updated_at = ? "
                "WHERE draft_id = ?",
                (_now(), row["draft_id"]),
            )
        return {
            "draft_id": row["draft_id"],
            # 被正式排期挤掉的草稿本就不占位，不能声称释放了时段
            "released_slot": row["slot"] if released else None,
            "version": row["version"] + 1,
        }

    # ---- 保存 ----
    def save_draft(
        self,
        material_id: str,
        account: str,
        slot: str,
        status: str,
        note: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        draft_id = f"D-{material_id}"
        try:
            with self._tx() as conn:
                existing = conn.execute(
                    "SELECT * FROM drafts WHERE material_id = ?", (material_id,)
                ).fetchone()
                if existing and existing["account"] != account:
                    existing = None  # 账号变了：不算幂等命中，按更新处理
                # 幂等：同一素材、同一时段、同一状态且占位仍在 → 不写入，返回原草稿
                if existing and existing["slot"] == slot and existing["status"] == status:
                    held = conn.execute(
                        "SELECT 1 FROM slots WHERE account = ? AND slot = ? AND kind = 'draft' "
                        "AND material_id = ?",
                        (account, slot, material_id),
                    ).fetchone()
                    if held:
                        return {
                            "ok": True,
                            "status": "unchanged",
                            "draft_id": existing["draft_id"],
                            "version": existing["version"],
                            "slot": slot,
                            "draft_status": status,
                        }
                conn.execute(
                    "DELETE FROM slots WHERE kind = 'draft' AND material_id = ?", (material_id,)
                )
                try:
                    conn.execute(
                        "INSERT INTO slots (account, slot, kind, material_id, draft_id) "
                        "VALUES (?, ?, 'draft', ?, ?)",
                        (account, slot, material_id, draft_id),
                    )
                except sqlite3.IntegrityError:
                    holder = conn.execute(
                        "SELECT kind, material_id, draft_id, title FROM slots "
                        "WHERE account = ? AND slot = ?",
                        (account, slot),
                    ).fetchone()
                    raise _Conflict(dict(holder)) from None
                conn.execute(
                    "INSERT INTO drafts (draft_id, material_id, account, slot, status, version, "
                    "request_id, note, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT (draft_id) DO UPDATE SET account = excluded.account, slot = excluded.slot, "
                    "status = excluded.status, version = drafts.version + 1, "
                    "request_id = excluded.request_id, note = excluded.note, "
                    "updated_at = excluded.updated_at",
                    (draft_id, material_id, account, slot, status, request_id, note, _now()),
                )
                version = conn.execute(
                    "SELECT version FROM drafts WHERE draft_id = ?", (draft_id,)
                ).fetchone()[0]
                if self.fault:
                    # 写入已执行、提交之前失败：验证整个事务回滚，不留半成品
                    self.fault = None if self.fault == "once" else self.fault
                    raise sqlite3.OperationalError("injected fault: disk I/O error")
        except _Conflict as c:
            return {"ok": False, "status": "conflict", "slot": slot, "holder": c.holder}
        except sqlite3.OperationalError as exc:  # 锁等待超时等存储错误：失败，绝不报成功
            return {"ok": False, "status": "failed", "error": "STORAGE_ERROR", "detail": str(exc)}
        result = {
            "ok": True,
            "status": "saved" if version == 1 else "updated",
            "draft_id": draft_id,
            "version": version,
            "slot": slot,
            "draft_status": status,
        }
        if existing and version > 1:
            result["previous"] = {"slot": existing["slot"], "status": existing["status"]}
        return result


class _Conflict(Exception):
    def __init__(self, holder: dict):
        self.holder = holder
