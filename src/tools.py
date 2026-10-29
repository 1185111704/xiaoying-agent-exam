"""工具层：模型可调用的 8 个工具、参数校验、调用记录（ledger）。

骨架阶段：工具返回固定结果，只用于跑通链路；各切片逐个替换为真实实现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Store

REASON = {"type": "string", "description": "本步的简要决策依据（会记入 trace）"}


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {**props, "reason": REASON},
                "required": [*required, "reason"],
            },
        },
    }


MID = {"type": "string", "description": "素材 ID，如 M01"}

TOOL_SPECS: list[dict] = [
    _fn(
        "read_material",
        "读取素材元数据。返回按档位标注的缺失字段（reject=无法排期 / lookup=可到版权库补查 / "
        "todo=不影响排期），以及该素材是否已在正式排期中。",
        {"material_id": MID},
        ["material_id"],
    ),
    _fn(
        "lookup_license",
        "到版权库补查授权记录。kind=music 查音乐授权，kind=portrait 查肖像授权。",
        {"material_id": MID, "kind": {"type": "string", "enum": ["music", "portrait"]}},
        ["material_id", "kind"],
    ),
    _fn(
        "check_rules",
        "对素材文案做规则检查，返回命中的规则、严重度（block/review）与原文证据。",
        {"material_id": MID},
        ["material_id"],
    ),
    _fn(
        "query_slots",
        "查询某账号在某天的时段占用。每个时段返回 free / scheduled（正式排期）/ held（其他草稿占位）"
        "及占用者。本素材自己的草稿占位视为 free。结果只作参考，以保存时为准。",
        {
            "account": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "material_id": MID,
        },
        ["account", "date", "material_id"],
    ),
    _fn(
        "save_draft",
        "保存（或更新）该素材的排期草稿并占用时段。同一素材只有一份草稿。若时段与用户要求不同，"
        "必须设置 needs_confirmation=true。只保存草稿，不会发布。",
        {
            "material_id": MID,
            "slot": {"type": "string", "description": "YYYY-MM-DD HH:MM，整点"},
            "needs_confirmation": {"type": "boolean"},
            "note": {"type": "string", "description": "给用户的说明"},
        },
        ["material_id", "slot", "needs_confirmation"],
    ),
    _fn(
        "request_confirmation",
        "登记一个需要用户拍板的问题（如时段调整、授权缺失、硬性要求冲突）。",
        {
            "material_id": MID,
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        ["material_id", "question"],
    ),
    _fn(
        "block",
        "阻断该素材：本次不排期，并说明原因与证据。",
        {"material_id": MID, "reason_code": {"type": "string"}, "evidence": {"type": "string"}},
        ["material_id", "reason_code"],
    ),
    _fn(
        "finish",
        "结束本次任务。每个素材都必须已有结论（已保存草稿 / 已阻断 / 已请求确认 / 保存失败）。",
        {"summary": {"type": "string", "description": "给用户的总结"}},
        ["summary"],
    ),
]

SPEC_BY_NAME = {s["function"]["name"]: s["function"]["parameters"] for s in TOOL_SPECS}


@dataclass
class LedgerEntry:
    step: int
    tool: str
    args: dict[str, Any]
    reason: str
    result: dict[str, Any]


@dataclass
class Ledger:
    """全部工具调用的原始记录。报告只从这里汇总，不采信模型的自述。"""

    entries: list[LedgerEntry] = field(default_factory=list)

    def for_material(self, material_id: str, tool: str | None = None) -> list[LedgerEntry]:
        return [
            e
            for e in self.entries
            if e.args.get("material_id") == material_id and (tool is None or e.tool == tool)
        ]


# bool 是 int 的子类，这里只用到 string/boolean/array，不涉及 integer
_TYPES = {"string": str, "boolean": bool, "array": list}


def validate_args(name: str, args: dict[str, Any]) -> str | None:
    """返回错误描述；参数合法时返回 None。"""
    spec = SPEC_BY_NAME.get(name)
    if spec is None:
        return f"未知工具 {name}"
    if "__raw__" in args:
        return "参数不是合法的 JSON 对象"
    unknown = sorted(set(args) - set(spec["properties"]))
    if unknown:
        return f"未知参数 {unknown}"
    missing = [k for k in spec["required"] if k not in args or args[k] in (None, "")]
    if missing:
        return f"缺少参数 {missing}"
    for key, value in args.items():
        prop = spec["properties"][key]
        expected = _TYPES.get(prop.get("type"))
        if value is not None and expected and not isinstance(value, expected):
            return f"参数 {key} 类型须为 {prop['type']}"
        if "enum" in prop and value not in prop["enum"]:
            return f"参数 {key} 取值须为 {prop['enum']}"
    return None


def _get(record: dict, dotted: str) -> Any:
    for part in dotted.split("."):
        record = record.get(part) if isinstance(record, dict) else None
    return record


def _load(data_dir: Path, name: str) -> Any:
    return json.loads((data_dir / name).read_text(encoding="utf-8"))


class ToolBox:
    def __init__(self, request: dict, data_dir: Path, store: Store):
        self.request = request
        self.items = {it["material_id"]: it for it in request["items"]}
        self.materials = _load(data_dir, "materials.json")
        self.rules = _load(data_dir, "rules.json")
        self.schedule = _load(data_dir, "schedule.json")
        self.rights = _load(data_dir, "rights.json")
        self.store = store
        # 启动时把正式排期同步进占位表；被挤掉的草稿在报告中提示
        self.displaced = store.sync_schedule(self.schedule)
        self.ledger = Ledger()
        self.finished = False

    # ---- 分发 ----
    def call(self, step: int, name: str, args: Any) -> dict[str, Any]:
        args = dict(args) if isinstance(args, dict) else {"__raw__": args}
        reason = args.pop("reason", "")
        error = validate_args(name, {**args, "reason": reason or None})
        if error:
            result = {"ok": False, "error": "INVALID_ARGS", "detail": error}
        else:
            try:
                result = getattr(self, f"_{name}")(**args)
            except Exception as exc:  # 工具内部异常：作为失败观察返回，不让整次运行崩溃
                result = {
                    "ok": False,
                    "status": "failed",
                    "error": "TOOL_ERROR",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
        reason = reason if isinstance(reason, str) else str(reason)
        self.ledger.entries.append(LedgerEntry(step, name, args, reason, result))
        return result

    def _not_in_request(self, material_id: str) -> dict | None:
        if material_id not in self.items:
            return {
                "ok": False,
                "error": "NOT_IN_REQUEST",
                "detail": f"{material_id} 不在本次请求中",
            }
        return None

    def _account(self, material_id: str) -> str | None:
        return (self.materials.get(material_id) or {}).get("account")

    def _slot_error(self, slot: str) -> str | None:
        """时段须为 YYYY-MM-DD HH:00，且在可发布小时内。"""
        try:
            t = datetime.strptime(slot, "%Y-%m-%d %H:%M")
        except ValueError:
            return "时段格式须为 YYYY-MM-DD HH:MM"
        if t.strftime("%Y-%m-%d %H:%M") != slot:  # strptime 接受不补零写法，会绕过主键冲突
            return "时段须为补零的标准写法 YYYY-MM-DD HH:MM，如 2026-11-03 09:00"
        lo, hi = self.rules["publish_hours"]
        if t.minute != 0 or not lo <= t.hour <= hi:
            return f"时段须为整点，且在 {lo:02d}:00–{hi:02d}:00 之间"
        return None

    # ---- 工具 ----
    def _read_material(self, material_id: str) -> dict:
        if err := self._not_in_request(material_id):
            return err
        m = self.materials.get(material_id)
        if m is None:
            return {"ok": False, "error": "MATERIAL_NOT_FOUND", "detail": "素材库中不存在"}
        fields = self.rules["fields"]
        missing = {
            "reject": {k: v for k, v in fields["reject"].items() if not _get(m, k)},
            "lookup": {
                k: v
                for k, v in fields["lookup"].items()
                if _get(m, k.split(".")[0] + (".used" if k.startswith("music") else ".present"))
                and not _get(m, k)
            },
            "todo": {k: v for k, v in fields["todo"].items() if not _get(m, k)},
        }
        scheduled = [e for e in self.schedule if e["material_id"] == material_id]
        draft = self.store.get_draft(material_id, m["account"]) if m.get("account") else None
        return {
            "ok": True,
            "material": m,
            "missing": {k: v for k, v in missing.items() if v},
            "already_scheduled": scheduled or None,
            "existing_draft": {k: draft[k] for k in ("draft_id", "slot", "status", "version")}
            if draft
            else None,
        }

    def _lookup_license(self, material_id: str, kind: str) -> dict:
        if err := self._not_in_request(material_id):
            return err
        record = self.rights.get(kind, {}).get(material_id)
        if record is None:
            return {"ok": True, "status": "not_found", "kind": kind}
        # 授权须覆盖要求的发布日期，否则视为过期（客观可判定）
        publish_date = self.items[material_id]["publish_at"][:10]
        if record.get("valid_until") and record["valid_until"] < publish_date:
            return {"ok": True, "status": "expired", "kind": kind, "record": record}
        return {"ok": True, "status": "found", "kind": kind, "record": record}

    def _rule_hits(self, material: dict) -> list[dict]:
        text = " ".join(str(material.get(k) or "") for k in ("title", "caption"))
        hits = []
        for rule in self.rules["terms"]:
            for word in rule["words"]:
                pos = text.find(word)
                if pos >= 0:
                    hits.append(
                        {
                            "rule": rule["rule"],
                            "desc": rule["desc"],
                            "severity": rule["severity"],
                            "evidence": text[max(0, pos - 8) : pos + len(word) + 8],
                            "matched": word,
                        }
                    )
        return hits

    def _check_rules(self, material_id: str) -> dict:
        if err := self._not_in_request(material_id):
            return err
        m = self.materials.get(material_id)
        if m is None:
            return {"ok": False, "error": "MATERIAL_NOT_FOUND", "detail": "素材库中不存在"}
        return {"ok": True, "hits": self._rule_hits(m)}

    def _query_slots(self, account: str, date: str, material_id: str) -> dict:
        try:
            if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
                raise ValueError
        except ValueError:
            return {"ok": False, "error": "INVALID_ARGS", "detail": "date 须为补零的 YYYY-MM-DD"}
        occupied = self.store.occupancy(account, date)
        lo, hi = self.rules["publish_hours"]
        slots = []
        for hour in range(lo, hi + 1):
            slot = f"{date} {hour:02d}:00"
            row = occupied.get(slot)
            if row is None or (row["kind"] == "draft" and row["material_id"] == material_id):
                slots.append({"slot": slot, "status": "free"})
            else:
                slots.append(
                    {
                        "slot": slot,
                        "status": "scheduled" if row["kind"] == "scheduled" else "held",
                        "holder": row["material_id"],
                        "title": row.get("title"),
                    }
                )
        return {"ok": True, "account": account, "slots": slots}

    def _save_draft(
        self, material_id: str, slot: str, needs_confirmation: bool, note: str = ""
    ) -> dict:
        if err := self._not_in_request(material_id):
            return err
        account = self._account(material_id)
        if not account:
            return {
                "ok": False,
                "status": "rejected",
                "error": "NO_ACCOUNT",
                "detail": "素材缺少发布账号，无法排期",
            }
        if err := self._slot_error(slot):
            return {"ok": False, "status": "rejected", "error": "INVALID_SLOT", "detail": err}
        if rejection := self._guard_save(material_id, slot, needs_confirmation):
            return {"ok": False, "status": "rejected", **rejection}
        status = "pending_confirmation" if needs_confirmation else "draft"
        context = self._occupancy_context(material_id) if needs_confirmation else None
        result = self.store.save_draft(
            material_id, account, slot, status, note, self.request.get("request_id")
        )
        if result.get("ok") and context:
            result["context"] = context
        return result

    def _guard_save(self, material_id: str, slot: str, needs_confirmation: bool) -> dict | None:
        """保存守卫：只做否决，返回拒绝原因交还模型；不替模型选择下一步。

        判定全部基于客观事实（素材数据、规则、请求、本次运行中已发生的工具调用）。
        """
        item = self.items[material_id]
        m = self.materials.get(material_id)
        if m is None:
            return {"error": "MATERIAL_NOT_FOUND", "detail": "素材库中不存在，不能排期"}
        if any(e["material_id"] == material_id for e in self.schedule):
            return {"error": "ALREADY_SCHEDULED", "detail": "素材已在正式排期中，属于重复提交"}
        if any(e.result.get("ok") for e in self.ledger.for_material(material_id, "block")):
            return {"error": "BLOCKED", "detail": "素材已被阻断，不能再排期"}

        missing = self._read_material(material_id)["missing"]
        if missing.get("reject"):
            return {"error": "FIELD_MISSING", "detail": f"缺少必需字段 {list(missing['reject'])}"}
        verified = {
            e.args.get("kind")
            for e in self.ledger.for_material(material_id, "lookup_license")
            if e.result.get("status") == "found"
        }
        unverified = [
            f
            for f in missing.get("lookup", {})
            if ("music" if f.startswith("music") else "portrait") not in verified
        ]
        if unverified:
            return {
                "error": "LICENSE_UNVERIFIED",
                "detail": f"授权未核实 {unverified}，需先用 lookup_license 补查",
            }
        if not any(
            e.result.get("ok") for e in self.ledger.for_material(material_id, "check_rules")
        ):
            return {"error": "NOT_CHECKED", "detail": "尚未做规则检查（check_rules）"}
        blocking = [h for h in self._rule_hits(m) if h["severity"] == "block"]
        if blocking:
            return {
                "error": "RULE_BLOCKED",
                "detail": f"命中阻断级规则 {blocking[0]['rule']}：{blocking[0]['evidence']}",
            }

        requested = item["publish_at"]
        changed = slot != requested
        if changed and item.get("fixed"):
            return {
                "error": "FIXED_REQUIREMENT",
                "detail": f"{requested} 是硬性要求，未经用户确认不得改动；"
                "请用 request_confirmation 交用户决定",
            }
        window = item.get("window")
        if not window and slot < requested:
            return {
                "error": "EARLIER_THAN_REQUESTED",
                "detail": f"{slot} 早于要求 {requested}；提前发布可能违反活动或保密时间，"
                "只能顺延，或请求用户确认（用户可在请求中给出允许提前的 window）",
            }
        if window and not window[0] <= slot <= window[1]:
            return {
                "error": "OUTSIDE_WINDOW",
                "detail": f"{slot} 超出用户给定窗口 {window[0]} – {window[1]}，未经确认不得改动",
            }
        if changed and not needs_confirmation:
            return {
                "error": "CONFIRMATION_REQUIRED",
                "detail": f"时段 {slot} 与要求 {requested} 不同，必须设置 needs_confirmation=true",
            }
        if not changed and needs_confirmation:
            return {
                "error": "CONFIRMATION_MISMATCH",
                "detail": "时段与要求一致，无需标记时段确认；其他待确认事项请用 request_confirmation",
            }
        return None

    def _occupancy_context(self, material_id: str) -> dict | None:
        """要求时段所在账号当天的占用清单 + 离要求时间最近的空闲时段（客观事实，由程序附上）。"""
        account = self._account(material_id)
        requested = self.items[material_id]["publish_at"]
        if not account or self._slot_error(requested):
            return None
        slots = self._query_slots(account, requested[:10], material_id)["slots"]
        occupied = [
            {
                "slot": s["slot"],
                "by": s["holder"],
                "title": s.get("title"),
                "kind": "正式排期" if s["status"] == "scheduled" else "其他草稿",
            }
            for s in slots
            if s["status"] != "free"
        ]
        window = self.items[material_id].get("window")
        lo, hi = window if window else (requested, f"{requested[:10]} 23:59")
        free = [
            s["slot"]
            for s in slots
            if s["status"] == "free" and s["slot"] != requested and lo <= s["slot"] <= hi
        ]
        hour = int(requested[11:13])
        # 可用候选（守卫会放行的）由近到远；距离相同按时间先后
        nearest = sorted(free, key=lambda s: (abs(int(s[11:13]) - hour), s))[:3]
        requested_by = next((o for o in occupied if o["slot"] == requested), None)
        return {
            "account": account,
            "requested_slot": requested,
            "requested_slot_occupied_by": requested_by,
            "occupied": occupied,
            "nearest_free": nearest,
        }

    def _request_confirmation(
        self, material_id: str, question: str, options: list[str] | None = None
    ) -> dict:
        if err := self._not_in_request(material_id):
            return err
        n = len([e for e in self.ledger.entries if e.tool == "request_confirmation"]) + 1
        return {
            "ok": True,
            "confirmation_id": f"C{n}",
            "context": self._occupancy_context(material_id),
        }

    def _block(self, material_id: str, reason_code: str, evidence: str = "") -> dict:
        if err := self._not_in_request(material_id):
            return err
        account = self._account(material_id)
        # 阻断时撤销该素材已有的草稿并释放时段，避免"已阻断"与"草稿占位"并存
        cancelled = self.store.cancel_draft(material_id, account) if account else None
        return {"ok": True, "blocked": material_id, "draft_cancelled": cancelled}

    def _unresolved(self) -> list[str]:
        """还没有结论的素材：本次运行没有成功保存（含未变化）/阻断/请求确认/保存失败。

        库中上一轮留下的草稿不算本轮结论：素材数据可能已变化，必须在本轮重新审核；
        重新保存会再过一遍保存守卫（含本轮规则检查），保存结果为 unchanged 时不产生写入。
        """
        return [
            mid
            for mid in self.items
            if not any(
                (e.tool in ("block", "request_confirmation") and e.result.get("ok"))
                or (
                    e.tool == "save_draft"
                    and e.result.get("status") in ("saved", "unchanged", "updated", "failed")
                )
                for e in self.ledger.for_material(mid)
            )
        ]

    def _finish(self, summary: str) -> dict:
        if unresolved := self._unresolved():
            return {
                "ok": False,
                "error": "UNRESOLVED_ITEMS",
                "detail": "以下素材还没有结论（保存草稿 / 阻断 / 请求确认 / 保存失败），不能结束",
                "unresolved": unresolved,
            }
        self.finished = True
        return {"ok": True}
