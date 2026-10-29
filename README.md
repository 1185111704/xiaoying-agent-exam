# 素材审核与发布排期 Agent

小影校招笔试 · **项目题 1**。帮助内容团队审核待发布素材，形成无冲突、可确认的排期**草稿**（系统没有发布能力）。

- Python 原生实现 ReAct 循环（不依赖 Agent 框架），OpenAI 兼容接口接 DeepSeek（`deepseek-flash`）
- 设计原则：**程序只否决，不替模型选路线**。冲突、确认边界、防重复、保存结果由程序保证；补查、换时段、请求确认、阻断、失败后是否重试由模型决定
- 运行方式：CLI

## 环境与安装

Python 3.10+。

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

- 离线 Mock 与全部测试**无需任何配置**。
- 真实模型：`cp .env.example .env`，填入 `LLM_API_KEY`（DeepSeek）。也可通过 `LLM_BASE_URL` / `LLM_MODEL` 切换到其他 OpenAI 兼容服务。

## 运行

```sh
# 单个请求（默认离线 Mock；加 --policy llm 使用真实模型）
python -m src.main examples/04-conflict/input.json --db runtime/demo.db
python -m src.main examples/04-conflict/input.json --db runtime/demo.db --policy llm

# 一键运行全部 8 个用例（12 次运行），输出写入 examples/<用例>/output.<policy>*.json
python scripts/run_examples.py --policy mock
python scripts/run_examples.py --policy llm

# 测试与静态检查（与 CI 相同）
pytest -q
ruff check . && ruff format --check .
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--policy mock\|llm` | 离线 Mock（默认）/ 真实模型 |
| `--db PATH` | 状态库（草稿与占位），跨运行保留；默认 `runtime/state.db` |
| `--data-dir PATH` | 替换合成数据（素材、排期、版权库、规则）后重跑 |
| `--fault-save always\|once` | 故障注入：保存草稿时持续 / 仅第一次失败 |
| `--max-steps N` / `--max-tool-calls N` | 执行上限（默认 15 / 30），达到上限返回 `incomplete` |
| `--out PATH` | 报告另存为文件 |

输出为 JSON 报告（每项素材的审核状态、风险原因、建议时段、待确认项、草稿保存状态、`publish_status=not_published`，以及完整 trace）。退出码：`0` 完成，`2` 等待确认，`1` 保存失败 / 未完成 / 错误。

> **Mock 边界**：`--policy mock` 用固定启发式代替模型，只证明"循环 + 工具 + 守卫 + 报告"能走通，**不证明真实模型的决策效果**。真实决策效果见 `examples/*/output.llm*.json`。

## 评审导航

| 想看什么 | 入口 |
|---|---|
| 需求、假设与成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策、方案对比、实现细节清单 | [docs/02-solution.md](docs/02-solution.md) |
| 验收点 ↔ 示例 ↔ 测试，真实运行结果与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 使用范围、个人决策与验证 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难问题、收获、下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 两次里程碑代码审查记录 | [docs/reviews/](docs/reviews/) |
| 真实模型运行记录（含 trace） | `examples/<用例>/output.llm*.json`，汇总见 [examples/summary.llm.txt](examples/summary.llm.txt) |
| 测试结果 | [examples/test-results.txt](examples/test-results.txt) |

## 目录

```text
src/
  agent.py         ReAct 循环、步数与工具调用上限、trace
  tools.py         8 个工具、参数校验、保存守卫
  store.py         SQLite：时段占位（唯一约束）、草稿、事务、故障注入
  report.py        报告汇总（只采信工具记录与数据库，校验模型总结）
  policies.py      真实模型（OpenAI 兼容）与脚本化假模型
  mock_policy.py   离线 Mock
  main.py          CLI 与输入校验
tests/             75 项测试（守卫、幂等、并发、失败回滚、报告来源、健壮性）
examples/          8 个用例的输入、真实模型与 Mock 输出、汇总、测试结果
data/              合成素材、正式排期、版权库、审核规则
scripts/           run_examples.py
docs/              01–05 文档与审查记录
```

## 开发流程

先搭可运行的行走骨架（walking skeleton），再按验收点做纵向切片（vertical slice），每个切片一个 PR；CI（GitHub Actions）强制 ruff + pytest；两个里程碑做多 agent 冷启动代码审查，审查结论落盘在 `docs/reviews/`。详见 [02-solution §11](docs/02-solution.md#11-开发流程)。
