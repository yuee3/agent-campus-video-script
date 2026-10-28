# 短视频脚本 Agent

小影校园 AI Agent 开发岗笔试 · **题目四：短视频脚本 Agent**。

帮助创作者生成**有事实依据、满足时长限制的可编辑脚本**：检索产品素材 → 创作脚本（产品事实逐字引用素材原文）→ 时长估算与品牌规则检查 → **超时长则压缩重写并重新估算**（无法满足时明确失败并说明缺口）。Agent 基于 ReAct 循环动态决策（改写复查 / 压缩修正 / 复检 / 明确失败）。

- 运行方式：CLI（Python 3.10+，零第三方依赖，无需密钥）
- 模型入口：离线确定性 Mock（默认）+ OpenAI 兼容 API（未随仓库验证，边界见下）
- 测试：11 个用例覆盖 5 个必验收点 + 编造红线 + 内容替换鲁棒性 + 步数上限 + CLI 边界，全部通过（[examples/test-results.txt](examples/test-results.txt)）

## 快速运行

在仓库根目录执行：

```bash
# 6 个验收场景（输出 JSON 到 stdout）
python -m src.main --input examples/normal.input.json       # 正常生成 -> delivered（首稿 81 秒在 90 秒内，检索->估算->检查->交付）
python -m src.main --input examples/over-duration.input.json # 超时长修正（参考实例）-> 首稿 47 秒超出 30 秒 -> 压缩 -> 29 秒交付（含分镜、P01 引用、总时长）
python -m src.main --input examples/cannot-meet.input.json   # 目标 10 秒压缩后仍超 -> cannot_meet（明确失败 + 缺口 19 秒，不交付）
python -m src.main --input examples/missing-info.input.json  # 缺目标时长 -> missing_input（0 次工具调用，返回缺口）
python -m src.main --input examples/no-material.input.json   # 无素材 -> no_material（两轮检索确认，请求补充，不编造）
python -m src.main --input examples/tool-failure.input.json  # 工具失败 -> 注入一次检索失败，重试续跑后正常交付

# 自动化测试
python -m unittest discover -s tests -v
```

带 Trace 输出（每步含工具参数——**含每版脚本文本，可逐版 diff 压缩前后**、观察结果、决策依据）：

```bash
python -m src.main --input examples/over-duration.input.json --trace-out examples/my-trace.json
```

约定：程序成功输出 JSON；业务异常（缺信息/无素材/无法满足时长/工具失败/未完成）以 JSON 状态返回且退出码 0；输入文件不是合法 JSON 时返回非零退出码。`data/` 为只读合成数据，运行不修改它；替换素材事实或目标时长，脚本与动作会随之改变（tests::test_8 验证）。

## 配置

复制 `.env.example` 为 `.env`（可选）。`AGENT_MODEL=mock`（默认）时**无需任何密钥**；`AGENT_MODEL=api` 时需配置 `OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL`。

> **Mock 边界**：Mock 模型是以"任务+历史观察"为输入的确定性策略函数，验证 ReAct 流程与工具协议，**不证明真实模型效果**——脚本创作、超时压缩、缺口计算、规则修正全部由同一策略根据观察结果推导，而非按场景硬编码。examples/ 中的全部运行记录均来自 Mock。

## 项目结构

```
├── README.md
├── requirements.txt            # 无第三方依赖声明
├── .env.example                # 变量清单，不含真实密钥
├── src/
│   ├── main.py                 # CLI 入口与输入文件校验
│   ├── agent.py                # ReAct 循环（步数上限/错误恢复/停止条件）
│   ├── model_client.py         # Mock 策略（含脚本模板与版本化自查）+ OpenAI 兼容 API 双入口
│   ├── tools.py                # 3 个业务工具（素材检索/时长估算/规则检查，故障注入）
│   ├── prompts.py              # 系统提示词（任务边界/事实纪律/判断职责/输出契约）
│   └── trace.py                # Trace 记录
├── tests/test_acceptance.py    # 5 验收点 + 编造红线 + 内容替换鲁棒性 + CLI 边界
├── data/                       # products.json（素材库）/ brand_rules.json（品牌规则）
├── examples/                   # 6 组输入/输出/Trace + 测试记录
└── docs/                       # 需求/方案/评估/AI 使用/复盘
```

## 评审导航

| 检查项 | 入口 |
| --- | --- |
| 业务目标与成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 测试预期、实际结果、失败与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助范围与验证方式 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难问题、学习收获、下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 超时长修正（参考实例：47→29 秒，P01 引用） | [examples/over-duration.trace.json](examples/over-duration.trace.json) |
| 明确失败与缺口（不交付超时脚本） | [examples/cannot-meet.output.json](examples/cannot-meet.output.json) |
| 编造红线（无"效率提升 80%"类结论） | tests/test_acceptance.py::test_7 |
| 改变时长/素材动作随之调整 | tests/test_acceptance.py::test_8 |

## 提交信息

- 题号：题目四（短视频脚本 Agent）
- 提交物：本仓库全部内容（代码、文档、合成数据、运行记录），不含真实密钥与隐私数据
- 评审可复现：按"快速运行"命令从零执行，结果应与 examples/ 一致（run_id、时间戳除外）
