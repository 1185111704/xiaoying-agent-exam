"""工具层：模型可调用的 8 个工具、参数校验、调用记录（ledger）。

骨架阶段：工具返回固定结果，只用于跑通链路；各切片逐个替换为真实实现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def validate_args(name: str, args: dict[str, Any]) -> str | None:
    """返回错误描述；参数合法时返回 None。"""
    spec = SPEC_BY_NAME.get(name)
    if spec is None:
        return f"未知工具 {name}"
    if "__raw__" in args:
        return "参数不是合法 JSON"
    missing = [k for k in spec["required"] if k not in args or args[k] in (None, "")]
    if missing:
        return f"缺少参数 {missing}"
    for key, prop in spec["properties"].items():
        if key in args and "enum" in prop and args[key] not in prop["enum"]:
            return f"参数 {key} 取值须为 {prop['enum']}"
    return None


class ToolBox:
    def __init__(self, request: dict, data_dir: Path):
        self.request = request
        self.items = {it["material_id"]: it for it in request["items"]}
        self.materials = json.loads((data_dir / "materials.json").read_text(encoding="utf-8"))
        self.ledger = Ledger()
        self.finished = False

    # ---- 分发 ----
    def call(self, step: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args)
        reason = str(args.pop("reason", ""))
        error = validate_args(name, {**args, "reason": reason or None})
        if error:
            result = {"ok": False, "error": "INVALID_ARGS", "detail": error}
        else:
            result = getattr(self, f"_{name}")(**args)
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

    # ---- 工具（骨架：固定返回） ----
    def _read_material(self, material_id: str) -> dict:
        if err := self._not_in_request(material_id):
            return err
        m = self.materials.get(material_id)
        if m is None:
            return {"ok": False, "error": "MATERIAL_NOT_FOUND"}
        return {"ok": True, "material": m, "missing": {}, "already_scheduled": None}

    def _lookup_license(self, material_id: str, kind: str) -> dict:
        return {"ok": True, "status": "not_found"}

    def _check_rules(self, material_id: str) -> dict:
        return {"ok": True, "hits": []}

    def _query_slots(self, account: str, date: str, material_id: str) -> dict:
        requested = self.items.get(material_id, {}).get("publish_at", f"{date} 18:00")
        return {"ok": True, "slots": [{"slot": requested, "status": "free"}]}

    def _save_draft(
        self, material_id: str, slot: str, needs_confirmation: bool, note: str = ""
    ) -> dict:
        if err := self._not_in_request(material_id):
            return err
        return {"ok": True, "status": "saved", "draft_id": f"D-{material_id}", "version": 1}

    def _request_confirmation(
        self, material_id: str, question: str, options: list[str] | None = None
    ) -> dict:
        n = len([e for e in self.ledger.entries if e.tool == "request_confirmation"]) + 1
        return {"ok": True, "confirmation_id": f"C{n}"}

    def _block(self, material_id: str, reason_code: str, evidence: str = "") -> dict:
        return {"ok": True, "blocked": material_id}

    def _finish(self, summary: str) -> dict:
        self.finished = True
        return {"ok": True}
