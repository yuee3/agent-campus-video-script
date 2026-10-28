# 03 - 评估（Evaluation）

评分方式：**确定性评分器**（unittest 断言）——输出结构化且行为可枚举，笔试范围内不需要 LLM-as-judge（留作下一步，见 05）。

## 测试执行方式

```bash
python -m unittest discover -s tests -v
```

实际运行记录：[examples/test-results.txt](../examples/test-results.txt)（11 个用例全部通过）。

## 用例、预期与实际结果

6 个必须验收点 + 步数边界 + 内容替换鲁棒性 + 2 个 CLI 边界，全部使用隔离临时数据目录，不污染仓库数据：

| # | 验收点 | 输入 | 预期行为 | 实际结果 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | 正常生成 | 咖啡杯，90 秒 | 检索→首稿 81 秒在时长内→规则检查通过→交付 | 通过：`delivered`，5 分镜，sources=[P02] | [normal.output.json](../examples/normal.output.json) / [normal.trace.json](../examples/normal.trace.json) |
| 2 | 超时长后的修正（参考实例） | 学习平板（P01 仅"支持字幕"），30 秒 | 首稿 47 秒超时→压缩→29 秒→交付含分镜、P01 引用、总时长 | 通过：估算恰 2 次，压缩决策依据可审计 | [over-duration.output.json](../examples/over-duration.output.json) / [over-duration.trace.json](../examples/over-duration.trace.json) |
| 3 | 超时长且无法满足 | 学习平板，10 秒 | 压缩后 29 秒仍超→明确失败+缺口，不交付 | 通过：`cannot_meet`，缺口"超出目标 10 秒约 19 秒" | [cannot-meet.output.json](../examples/cannot-meet.output.json) / [cannot-meet.trace.json](../examples/cannot-meet.trace.json) |
| 4 | 缺关键信息 | 无 target_duration | 不调用工具，返回缺口 | 通过：`missing_input`，0 次工具调用 | [missing-info.output.json](../examples/missing-info.output.json) / [missing-info.trace.json](../examples/missing-info.trace.json) |
| 5 | 无素材 | "校园无人机" | 两轮检索确认→请求补充，不编造 | 通过：`no_material`，恰 2 次检索 | [no-material.output.json](../examples/no-material.output.json) / [no-material.trace.json](../examples/no-material.trace.json) |
| 6 | 工具失败 | 注入 `search_fail_once` | 首次检索 failed→重试 success→正常交付 | 通过：steps 含 failed+success | [tool-failure.output.json](../examples/tool-failure.output.json) / [tool-failure.trace.json](../examples/tool-failure.trace.json) |
| 7 | 编造红线（参考实例） | 学习平板，60 秒 | 素材只有"支持字幕"，全脚本不得含"效率提升 80%"等百分比结论 | 通过：全文无 `\d+%`、无"效率提升" | tests::test_7 |
| 8 | 改变时长/素材，动作调整（审核要点） | 同素材 30 秒 vs 10 秒；给 P01 增加事实 | 30 秒→压缩交付；10 秒→明确失败；事实增加→分镜随之增加 | 通过 | tests::test_8 |
| 9 | 步数上限 | max_steps=3 | 明确 `incomplete` + 上限说明 | 通过 | tests::test_9 |
| 10-11 | CLI 边界 | 合法输入 / 非法 JSON | 退出码 0 + Trace 落盘 / 非零退出码 | 通过 | tests::CliTests |

## 红线断言（所有场景统一检查）

`tests/test_acceptance.py::assert_red_lines` 对每个场景断言：

1. **事实可追溯**：`delivered` 时事实分镜旁白逐字包含素材 facts 原文、`based_on`/`sources` 对应检索到的素材 ID、每镜 `est_sec` 之和 == `total_sec`（与估算工具逐一对应）；
2. **不编造**：全脚本（标题+旁白）不含无数据支撑的百分比结论；
3. **时长可靠**：`total_sec` ≤ 目标时长（超时脚本不交付）；
4. **非交付状态必须说明缺口且不交出脚本**；
5. **失败步骤按实际观察记录**（`success/failed`）；topic/target_duration 原样回显。

## 两条不同执行路径的运行记录

- **路径 A（一次通过）**：[normal.trace.json](../examples/normal.trace.json) —— 首稿 81 秒在 90 秒内 → 规则检查通过 → 交付（estimate 1 次）。
- **路径 B（超时修正）**：[over-duration.trace.json](../examples/over-duration.trace.json) —— 首稿 47 秒超出 30 秒 → 压缩重写 → 29 秒 → 检查 → 交付（estimate 2 次，决策依据写明"观察到首稿估算 47 秒超出目标时长 30 秒，压缩脚本并重新估算"）。

同一 Mock 策略在不同观察下走出不同路径，体现"时长或规则检查不通过时，调整脚本并再次检查，或说明无法满足"的动态决策。

## 已知限制

1. **Mock 边界**：examples/ 全部结果来自离线确定性 Mock 模型，只验证 ReAct 流程与工具协议，**不证明真实模型效果**；`AGENT_MODEL=api` 路径未随本仓库验证。
2. 时长估算是规则化近似（字数/语速 + 固定开销），未考虑语速变化、口播停顿、BGM 卡点等真实拍摄因素。
3. 脚本由模板 + 素材事实组装，文案多样性有限；规则修正只做词组替换（模板不含违禁词时该路径不触发）。
4. 检索为关键词 2-gram 打分，无语义召回；口语化产品别名可能漏检（此时如实返回无素材请求补充，不编造）。
5. 评分器为确定性检查，缺少 LLM-as-judge 对脚本文案质量的评估。
