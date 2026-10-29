"""最终报告：审核状态、风险、时段、待确认项、保存状态全部由程序从 ledger 汇总。

模型的文字只作为说明附带，并校验它是否与记录矛盾。
"""

from __future__ import annotations

from typing import Any

from .agent import RunResult
from .tools import Ledger

SAVED = {"saved", "unchanged", "updated"}


def _last(entries: list, pred=lambda e: True):
    for e in reversed(entries):
        if pred(e):
            return e
    return None


def _review(ledger: Ledger, mid: str) -> tuple[str, list[dict]]:
    reasons: list[dict] = []
    read = _last(ledger.for_material(mid, "read_material"), lambda e: "error" not in e.result)
    read_err = _last(ledger.for_material(mid, "read_material"))
    if read_err and read_err.result.get("error") == "MATERIAL_NOT_FOUND":
        return "info_missing", [{"code": "MATERIAL_NOT_FOUND", "detail": "素材库中不存在"}]
    if read is None:
        return "not_checked", []

    missing = read.result.get("missing", {})
    for field_name, desc in missing.get("reject", {}).items():
        reasons.append({"code": "FIELD_MISSING", "field": field_name, "detail": desc})
    found_kinds = {
        e.args.get("kind")
        for e in ledger.for_material(mid, "lookup_license")
        if e.result.get("status") == "found"
    }
    for field_name, desc in missing.get("lookup", {}).items():
        kind = "music" if field_name.startswith("music") else "portrait"
        if kind not in found_kinds:
            reasons.append({"code": "LICENSE_MISSING", "field": field_name, "detail": desc})

    check = _last(ledger.for_material(mid, "check_rules"), lambda e: e.result.get("ok"))
    hits = check.result.get("hits", []) if check else []
    for h in hits:
        reasons.append(
            {"code": h.get("rule"), "severity": h.get("severity"), "evidence": h.get("evidence")}
        )

    if any(h.get("severity") == "block" for h in hits):
        return "blocked", reasons
    if any(r["code"] in ("FIELD_MISSING", "LICENSE_MISSING") for r in reasons):
        return "info_missing", reasons
    if check is None:
        return "not_checked", reasons
    if hits:
        return "needs_review", reasons
    return "passed", reasons


def _item_report(ledger: Ledger, item: dict) -> dict[str, Any]:
    mid = item["material_id"]
    review_status, reasons = _review(ledger, mid)
    saves = ledger.for_material(mid, "save_draft")
    good = _last(saves, lambda e: e.result.get("status") in SAVED)
    last_save = _last(saves)
    blocked = _last(ledger.for_material(mid, "block"), lambda e: e.result.get("ok"))
    confirms = [e for e in ledger.for_material(mid, "request_confirmation") if e.result.get("ok")]

    pending = [
        {
            "confirmation_id": e.result["confirmation_id"],
            "question": e.args.get("question"),
            "options": e.args.get("options", []),
        }
        for e in confirms
    ]
    # 时段是否改动由程序比较得出，不采信模型自报的 needs_confirmation（骨架审查 #3）
    if good and not blocked and good.args["slot"] != item.get("publish_at"):
        pending.append(
            {
                "confirmation_id": None,
                "question": f"建议时段 {good.args['slot']} 与要求 {item.get('publish_at')} 不同，是否接受？",
            }
        )

    if blocked:
        outcome = "blocked"
    elif good:
        outcome = "draft_saved_pending_confirmation" if pending else "draft_saved"
    elif last_save and last_save.result.get("status") == "failed":
        outcome = "save_failed"
    elif confirms:
        outcome = "awaiting_confirmation"
    else:
        outcome = "unresolved"

    draft_src = good or last_save
    cancelled = blocked.result.get("draft_cancelled") if blocked else None
    if cancelled:
        draft = {
            "status": "cancelled",
            "draft_id": cancelled["draft_id"],
            "version": cancelled["version"],
            "released_slot": cancelled["released_slot"],
        }
    else:
        draft = {
            "status": draft_src.result.get("status", draft_src.result.get("error"))
            if draft_src
            else "not_saved",
            "draft_id": good.result.get("draft_id") if good else None,
            "version": good.result.get("version") if good else None,
        }
    return {
        "material_id": mid,
        "review_status": review_status,
        "risk_reasons": reasons,
        "requested_slot": item.get("publish_at"),
        "fixed": bool(item.get("fixed")),
        "suggested_slot": good.args["slot"] if good and not blocked else None,
        "draft": draft,
        "pending_confirmations": pending,
        "block": {
            "reason_code": blocked.args.get("reason_code"),
            "evidence": blocked.args.get("evidence"),
        }
        if blocked
        else None,
        "outcome": outcome,
        "publish_status": "not_published",
    }


def build_report(request: dict, ledger: Ledger, run: RunResult) -> dict[str, Any]:
    items = [_item_report(ledger, it) for it in request["items"]]
    outcomes = {i["outcome"] for i in items}

    if run.status == "error":
        task_status = "error"
    elif run.status in ("step_limit", "tool_limit") or "unresolved" in outcomes:
        task_status = "incomplete"
    elif "save_failed" in outcomes:
        task_status = "save_failed"
    elif any(i["pending_confirmations"] for i in items):
        task_status = "needs_confirmation"
    else:
        task_status = "completed"

    warnings = []
    text = run.final_message or ""
    if "已发布" in text:
        warnings.append(
            "模型总结中出现“已发布”，与系统能力不符；以本报告为准（publish_status=not_published）。"
        )
    if "save_failed" in outcomes and ("保存成功" in text or "已保存" in text):
        warnings.append("模型总结声称保存成功，但记录显示存在保存失败；以本报告为准。")

    trace = [dict(t) for t in run.trace]
    for i, t in enumerate(trace):
        t["next_action"] = trace[i + 1]["tool"] if i + 1 < len(trace) else "(end)"

    return {
        "request_id": request.get("request_id"),
        "task_status": task_status,
        "run": {
            "status": run.status,
            "steps": run.steps,
            "tool_calls": run.tool_calls,
            "error": run.error,
        },
        "items": items,
        "model_summary": run.final_message,
        "consistency_warnings": warnings,
        "trace": trace,
    }
