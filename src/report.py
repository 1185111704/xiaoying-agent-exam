"""最终报告：审核状态、风险、时段、待确认项、保存状态全部由程序从 ledger 汇总。

模型的文字只作为说明附带，并校验它是否与记录矛盾。
"""

from __future__ import annotations

import re
from typing import Any

from .agent import RunResult
from .tools import Ledger, ToolBox

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


ACTIVE = {"draft", "pending_confirmation", "confirmed"}


def _item_report(
    ledger: Ledger, item: dict, displaced: list[dict], state: dict | None
) -> dict[str, Any]:
    """state 为素材草稿在数据库中的最终状态（跨运行保留）；ledger 只说明本次运行做了什么。"""
    mid = item["material_id"]
    review_status, reasons = _review(ledger, mid)
    saves = ledger.for_material(mid, "save_draft")
    good = _last(saves, lambda e: e.result.get("status") in SAVED)
    last_save = _last(saves)
    blocked = _last(ledger.for_material(mid, "block"), lambda e: e.result.get("ok"))
    confirms = [e for e in ledger.for_material(mid, "request_confirmation") if e.result.get("ok")]
    active = state is not None and state["status"] in ACTIVE and state["holds_slot"]

    pending = [
        {
            "confirmation_id": e.result["confirmation_id"],
            "question": e.args.get("question"),
            "options": e.args.get("options", []),
            "context": e.result.get("context"),
        }
        for e in confirms
    ]
    # 时段是否改动由程序比较库中草稿与要求得出，不采信模型自报的 needs_confirmation（审查 1 #3）
    if active and state["slot"] != item.get("publish_at"):
        pending.append(
            {
                "confirmation_id": None,
                "question": f"建议时段 {state['slot']} 与要求 {item.get('publish_at')} 不同，是否接受？",
                "context": good.result.get("context") if good else None,
            }
        )

    if blocked:
        outcome = "blocked"
    elif last_save and last_save.result.get("status") == "failed" and not good:
        outcome = "save_failed"
    elif active:
        outcome = "draft_saved_pending_confirmation" if pending else "draft_saved"
    elif confirms:
        outcome = "awaiting_confirmation"
    else:
        outcome = "unresolved"

    notices = [
        f"上次草稿占用的 {d['slot']} 已被正式排期占用，本次重新安排"
        for d in displaced
        if d.get("material_id") == mid
    ]
    draft: dict[str, Any] = {
        "status": "not_saved",
        "draft_id": None,
        "slot": None,
        "version": None,
        "this_run": "none",
    }
    if state:
        draft = {
            "status": state["status"],
            "draft_id": state["draft_id"],
            "slot": state["slot"] if state["holds_slot"] else None,
            "version": state["version"],
            "this_run": "none",
        }
    if last_save:
        draft["this_run"] = last_save.result.get("status") or last_save.result.get("error")
    if good and good.result.get("previous"):
        draft["previous"] = good.result["previous"]
    cancelled = blocked.result.get("draft_cancelled") if blocked else None
    if cancelled:
        draft["this_run"] = "cancelled"
        draft["released_slot"] = cancelled["released_slot"]

    return {
        "material_id": mid,
        "review_status": review_status,
        "risk_reasons": reasons,
        "requested_slot": item.get("publish_at"),
        "fixed": bool(item.get("fixed")),
        "suggested_slot": state["slot"] if active and not blocked else None,
        "draft": draft,
        "pending_confirmations": pending,
        "block": {
            "reason_code": blocked.args.get("reason_code"),
            "evidence": blocked.args.get("evidence"),
        }
        if blocked
        else None,
        "outcome": outcome,
        "notices": notices,
        "publish_status": "not_published",
    }


def build_report(request: dict, toolbox: ToolBox, run: RunResult) -> dict[str, Any]:
    """草稿信息读数据库最终状态；审核、动作、待确认来自本次运行的工具记录。"""
    items = [
        _item_report(
            toolbox.ledger, it, toolbox.displaced, toolbox.store.draft_state(it["material_id"])
        )
        for it in request["items"]
    ]
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
    failed = [i["material_id"] for i in items if i["outcome"] == "save_failed"]
    for clause in re.split(r"[；;。，,\n]", text):
        claims_saved = ("保存成功" in clause or "已保存" in clause) and "失败" not in clause
        named = [m for m in failed if m in clause]
        # 点名了失败素材却说成功；或未点名任何素材、笼统声称成功
        mentions_any = any(i["material_id"] in clause for i in items)
        if claims_saved and (named or (failed and not mentions_any)):
            warnings.append(
                f"模型总结“{clause.strip()}”与记录不符（保存失败：{failed}）；以本报告为准。"
            )

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
