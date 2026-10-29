# 03 效果评估

验证分两层：

- **确定性测试**（`tests/`，77 项，脚本化假模型 + 直接调用工具）：证明守卫、幂等、并发、失败回滚、报告来源这些**程序保证**成立，不依赖模型表现。结果见 [examples/test-results.txt](../examples/test-results.txt)。
- **真实模型运行**（DeepSeek `deepseek-flash`，`temperature=0`）：8 个用例共 12 次运行，证明模型会根据工具结果调整动作。命令 `python scripts/run_examples.py --policy llm`，汇总见 [examples/summary.llm.txt](../examples/summary.llm.txt)，每次运行的完整报告与 trace 见 `examples/<用例>/output.llm*.json`。

离线 Mock 的同组输出（`output.mock*.json`、[summary.mock.txt](../examples/summary.mock.txt)）只证明流程可走通，不代表模型决策效果。

## 1. 验收点逐项结果（真实模型）

| 验收点 | 用例与文件 | 预期 | 实际 | 对应测试 |
|---|---|---|---|---|
| 正常排期 | `01-normal/output.llm.json` | M02 排进 10:00，无待确认 | ✅ `draft_saved`，草稿 D-M02 v1，退出码 0 | `test_skeleton`、`test_slice1_tools` |
| 素材信息缺失 | `02-missing-info/output.llm.json` | 缺文案打回；授权补查到则继续、补不到请求确认；素材不存在打回 | ✅ M03 阻断（缺文案）；M06 补查到 LIC-0606 → 按原时段 21:00 保存；M05 补查无记录 → 请求确认；M99 阻断（素材不存在） | `test_slice5_review` |
| 规则风险 | `03-rule-risk/output.llm.json` | M04 阻断，附规则与原文证据；M02 正常 | ✅ M04 阻断，命中 AD-01（"最强""第一""全网最"），证据为原文片段；M02 排进 11:00 | `test_slice2_guards::test_check_rules_returns_evidence` |
| 时段冲突 | `04-conflict/output.llm.json` | M01 改到替代时段并待确认；M07 硬性要求不改 | ✅ M01 → 19:00 待确认（先试 17:00 被守卫拒绝后顺延）；M07 未保存，请求确认，附占用清单 | `test_slice2_guards`、`test_guard_earlier_finish` |
| 重复提交 | `05-duplicate/output.llm.run1.json`、`run2.json` | 同一请求两次只有一份草稿；已排期素材被识别 | ✅ 第二次 M01 `this_run=unchanged`，仍为 D-M01 v1；M08 两次都被阻断（已在正式排期 12:00） | `test_slice3_idempotency` |
| 保存失败 | `06-save-failure/output.llm.always.json`、`once.json` | 明确返回失败，不称成功，占位回滚 | ✅ always：两次保存失败，`save_failed`，无草稿、无占位，退出码 1；once：首次失败后模型重试成功 | `test_slice4_save_failure` |
| 隐藏测试：更换占用时段并重复提交 | `07-resubmit/output.llm.run1/run2-19-taken/run3-18-freed.json` | 草稿始终一份，随排期变化调整 | ✅ 19:00（v1，待确认）→ 19:00 被占后 21:00（v2，提示被挤占）→ 18:00 释放后回到 18:00（v3，不再待确认） | `test_slice3_idempotency` |
| 步数上限 | `08-step-limit/output.llm.json` | 达到上限返回未完成 | ✅ `--max-steps 2`：`run.status=step_limit`，`task_status=incomplete`，4 个素材列为 `unresolved` | `test_skeleton`、`test_robustness` |

关键缺陷检查：12 次运行的 `publish_status` 均为 `not_published`；`consistency_warnings` 均为空（模型总结没有声称发布，也没有与记录矛盾的保存结论）。

## 2. 并发与幂等（确定性测试）

| 场景 | 结果 |
|---|---|
| 10 个线程同时抢同一时段 | 恰好 1 个成功、9 个冲突（本地连续 20 次一致） |
| 抢占失败 | 回滚后自己原来的占位仍在 |
| 不补零写法 `2026-11-3 18:00` | 被拒（`INVALID_SLOT`），无法绕过 (账号, 时段) 唯一约束 |
| 数据库锁等待超时 | 返回 `failed`，不报成功 |
| 写入后、提交前故障 | 整个事务回滚，无半成品；更新失败时旧草稿与旧占位保持不变 |
| 上一轮存下的草稿，本轮模型只请求确认 | 报告仍从数据库如实显示草稿与占位 |

## 3. 真实模型的动态决策

题目要求"至少两条执行路径，体现模型根据不同工具结果调整动作"。以下均摘自真实运行的 trace。

**路径 A：保存结果不同 → 后续动作不同**（`06-save-failure`，同一输入）

| 步骤 | once（偶发失败） | always（持续失败） |
|---|---|---|
| 第 1 次 `save_draft` | failed → 依据："首次保存返回 STORAGE_ERROR，判断可能是偶发故障，重试一次" | failed → 同样重试一次 |
| 第 2 次 `save_draft` | **saved** → 查时段确认已占位 → 结束，`completed` | **failed** → 查时段确认占位已回滚 → 结束，如实报告"保存失败" |

**路径 B：补查结果不同 → 后续动作不同**（`02-missing-info`，同一轮并行补查）

| 素材 | `lookup_license` 结果 | 下一步 |
|---|---|---|
| M06（音乐） | found（LIC-0606） | 规则检查 → 查时段 → 按原时段保存 |
| M05（肖像） | not_found | 请求确认，不排期 |

**路径 C：守卫拒绝 → 模型自行修正**（`04-conflict`、`07-resubmit` run2）

模型两次先选了早于要求的 17:00，被 `EARLIER_THAN_REQUESTED` 拒绝后，依据拒绝原因顺延："17:00 因早于要求时间被拒，只能顺延；18:00–20:00 均被正式排期占用，最近空闲为 21:00"。

**路径 D：提前结束被拦 → 模型补处理**（`05-duplicate` run1）

模型漏处理 M08 就调用 `finish`，返回 `UNRESOLVED_ITEMS`（列出 M08）；模型随后阻断 M08（已在正式排期中），再结束。

**路径 E：排期数据变化 → 时段决策变化**（`07-resubmit` 三次运行）：19:00 → 21:00 → 18:00，见 §1。

## 4. 发现的问题与处理

开发过程中通过以下方式发现问题，均已修复并补回归测试（边界类问题见审查记录）：

| 发现方式 | 问题 | 处理 |
|---|---|---|
| 冷启动多 agent 评审设计初稿 | 幂等键若包含模型选出的时段，重复提交时会与自己的旧草稿冲突、生成第二份草稿 | 改用业务键（素材, 账号），见 02 §6 |
| 里程碑代码审查 1（骨架） | 报告采信模型自报的确认标记；阻断后草稿未释放 | 是否需要确认由程序按时段事实判定；阻断时撤销草稿。见 [reviews/01-skeleton.md](reviews/01-skeleton.md) |
| 里程碑代码审查 2（保存层） | 不补零写法可绕过唯一约束；跨运行草稿的报告来源 | 时间只接受标准写法；报告读数据库最终状态。见 [reviews/02-save-layer.md](reviews/02-save-layer.md) |
| 阅读真实运行 trace | 模型把 18:00 的替代时段选在 17:00（提前发布），时间方向完全由模型决定 | 改为程序守卫：未给窗口时只能顺延；加守卫后重跑，模型被拒一次后顺延到 19:00 |
| 梳理错误处理链路 | 模型漏处理素材就结束时，应被拦回补处理 | 补全 `finish` 的结论检查（`UNRESOLVED_ITEMS`）；真实运行中已触发一次（路径 D） |
| 交付前冷启动阅卷评审 | 上一轮的草稿被当作本轮结论：素材改为违规后，本轮未复审直接结束仍报"已完成" | `finish` 只认本轮动作，旧草稿须本轮复审；报告如实标为未处理。见 [reviews/03-grader.md](reviews/03-grader.md) |

## 5. 已知限制

- **待确认项的占用情况是登记时的快照。** `04-conflict` 中 M07 在第 3 轮登记确认，当时 19:00 空闲，因此程序附带的最近空位与模型给的选项都含 19:00；第 4 轮 M01 存入 19:00 后，该选项已不可用。修法：报告生成时按数据库最终状态重新计算占用情况。
- **确认项偶有重复**：模型保存待确认草稿后，有时又登记一条内容相同的确认（`07-resubmit` run1、run2）。结构化字段（时段、状态、占用）由程序生成，不受影响；修法是程序对同一素材的确认项去重。
- **第二轮确认未实现**：待确认项可输出，但还不能用 `--confirm` 把草稿推进为已确认。
- **审核规则为示意**：只有词表检查与授权补查，且只有阻断级规则，"风险 → 请求确认"路径没有真实运行记录；无 LLM 语义审核。
- **总结一致性校验基于关键词**：换一种说法的虚报不会告警；报告的结构化字段以记录为准，不受影响。
- **真实模型样本量小**：每个用例只跑一次（`temperature=0`），未统计多次运行的稳定性。
- **单机 SQLite**：占位不会超时释放；未做多机部署。
