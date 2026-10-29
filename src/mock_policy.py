"""离线 Mock 策略：用固定启发式代替模型，供无 API Key 时演示流程。

边界：它只证明"循环 + 工具 + 守卫 + 报告"能走通，不证明真实模型的决策效果。
真实决策效果见 examples/ 中 --policy llm 的运行记录。
"""

from __future__ import annotations

import json
import uuid

from .policies import Decision, ToolCall, _assistant_message


def _last_result(messages: list[dict]) -> dict | None:
    if messages and messages[-1].get("role") == "tool":
        return json.loads(messages[-1]["content"])
    return None


def _nearest(slots: list[str], want: str, k: int = 3) -> list[str]:
    hour = int(want[11:13])
    return sorted(slots, key=lambda s: (abs(int(s[11:13]) - hour), s))[:k]


class MockPolicy:
    def __init__(self, request: dict):
        self.items = list(request["items"])
        self.i = 0
        self.state = "read"
        self.ctx: dict = {}
        self.done: list[str] = []

    # ---- 对外接口 ----
    def decide(self, messages: list[dict], tools: list[dict]) -> Decision:
        name, args = self._next(_last_result(messages))
        call = ToolCall(f"mock_{uuid.uuid4().hex[:8]}", name, args)
        return Decision(None, [call], _assistant_message(None, [call]))

    # ---- 启发式 ----
    def _advance(self, note: str):
        self.done.append(note)
        self.i += 1
        self.state, self.ctx = "read", {}
        return self._next(None)

    def _next(self, obs: dict | None) -> tuple[str, dict]:
        if self.i >= len(self.items):
            return "finish", {
                "summary": "；".join(self.done) or "无素材",
                "reason": "所有素材已有结论",
            }
        item = self.items[self.i]
        mid = item["material_id"]
        st = self.state

        if st == "read":
            self.state = "after_read"
            return "read_material", {"material_id": mid, "reason": "先读取素材元数据"}

        if st == "after_read":
            if not obs.get("ok"):
                self.state = "after_block"
                return "block", {
                    "material_id": mid,
                    "reason_code": obs.get("error", "READ_FAILED"),
                    "reason": "素材无法读取，阻断",
                }
            self.ctx["material"] = obs["material"]
            if obs.get("already_scheduled"):
                self.state = "after_block"
                return "block", {
                    "material_id": mid,
                    "reason_code": "ALREADY_SCHEDULED",
                    "evidence": json.dumps(obs["already_scheduled"], ensure_ascii=False),
                    "reason": "素材已在正式排期中，属于重复提交",
                }
            missing = obs.get("missing", {})
            if missing.get("reject"):
                self.state = "after_block"
                return "block", {
                    "material_id": mid,
                    "reason_code": "FIELD_MISSING",
                    "evidence": json.dumps(missing["reject"], ensure_ascii=False),
                    "reason": "缺少必需字段，无法排期",
                }
            self.ctx["lookups"] = [
                "music" if f.startswith("music") else "portrait" for f in missing.get("lookup", {})
            ]
            self.state = "lookup"
            return self._next(None)

        if st == "lookup":
            if obs is not None and obs.get("status") != "found":
                self.state = "after_confirm"
                kind = self.ctx["last_kind"]
                return "request_confirmation", {
                    "material_id": mid,
                    "question": f"版权库未找到{kind}授权，是否补充授权后再排期？",
                    "options": ["补充授权后重新提交", "放弃本次排期"],
                    "reason": "补查无结果，不能擅自排期，交用户决定",
                }
            if self.ctx["lookups"]:
                kind = self.ctx["lookups"].pop(0)
                self.ctx["last_kind"] = kind
                return "lookup_license", {
                    "material_id": mid,
                    "kind": kind,
                    "reason": f"素材库缺{kind}授权，到版权库补查",
                }
            self.state = "after_rules"
            return "check_rules", {"material_id": mid, "reason": "检查文案规则"}

        if st == "after_rules":
            hits = obs.get("hits", [])
            blocks = [h for h in hits if h.get("severity") == "block"]
            if blocks:
                self.state = "after_block"
                return "block", {
                    "material_id": mid,
                    "reason_code": blocks[0]["rule"],
                    "evidence": blocks[0].get("evidence", ""),
                    "reason": "命中阻断级规则",
                }
            if hits:
                self.state = "after_confirm"
                return "request_confirmation", {
                    "material_id": mid,
                    "question": "命中需人工复核的规则，是否仍要排期？",
                    "reason": "风险需人工判断",
                }
            self.state = "after_query"
            return self._query(item)

        if st == "after_query":
            return self._choose(item, obs)

        if st == "after_save":
            status = obs.get("status") or obs.get("error")
            if status in ("saved", "unchanged", "updated"):
                return self._advance(f"{mid} 草稿{status}于 {self.ctx['slot']}")
            if status == "conflict" and not self.ctx.get("requeried"):
                self.ctx["requeried"] = True
                self.ctx.setdefault("taken", set()).add(self.ctx["slot"])
                self.state = "after_query"
                return self._query(item)
            return self._advance(f"{mid} 保存未成功（{status}）")

        if st in ("after_block", "after_confirm"):
            return self._advance(f"{mid} 已{'阻断' if st == 'after_block' else '请求确认'}")

        raise RuntimeError(f"unknown state {st}")

    def _query(self, item: dict) -> tuple[str, dict]:
        return "query_slots", {
            "account": self.ctx["material"].get("account", ""),
            "date": item["publish_at"][:10],
            "material_id": item["material_id"],
            "reason": "查询该账号当天时段占用",
        }

    def _choose(self, item: dict, obs: dict) -> tuple[str, dict]:
        mid, want = item["material_id"], item["publish_at"]
        taken = self.ctx.get("taken", set())
        free = [
            s["slot"]
            for s in obs.get("slots", [])
            if s["status"] == "free" and s["slot"] not in taken
        ]
        lo, hi = item.get("window") or (want[:10] + " 00:00", want[:10] + " 23:59")
        in_window = [s for s in free if lo <= s <= hi]
        if want in in_window:
            slot, confirm = want, False
        elif item.get("fixed") or not in_window:
            occupied = [s for s in obs.get("slots", []) if s["status"] != "free"]
            self.state = "after_confirm"
            return "request_confirmation", {
                "material_id": mid,
                "question": f"要求时段 {want} 不可用"
                + ("（硬性要求，不能擅自调整）" if item.get("fixed") else "，窗口内无空位"),
                "options": [f"改到 {s}" for s in _nearest(free, want)] + ["取消本次排期"],
                "reason": f"不能擅自改动；占用情况：{json.dumps(occupied, ensure_ascii=False)}",
            }
        else:
            after = [s for s in in_window if s > want]
            slot, confirm = (after or in_window)[0], True
        self.ctx["slot"] = slot
        self.state = "after_save"
        return "save_draft", {
            "material_id": mid,
            "slot": slot,
            "needs_confirmation": confirm,
            "note": "" if not confirm else f"{want} 被占用，建议改为 {slot}",
            "reason": "要求时段可用" if not confirm else "要求时段被占，选最近的空闲时段",
        }
