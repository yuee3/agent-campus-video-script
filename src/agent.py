"""ReAct 循环（第二板斧：Agent Loop 执行骨架）。

循环：模型选定动作 -> 执行工具 -> 观察结果 -> 模型调整下一步或结束。
- 停止条件：模型 finish，或达到步数上限（超限返回明确的 incomplete 未完成状态）。
- 错误恢复：工具异常转为观察结果交回模型决策，不终止循环。
- 全过程写入 Trace（第三板斧：过程证据）。
"""
from __future__ import annotations

import uuid

from .trace import TraceRecorder


def run_agent(task: dict, client, registry, max_steps: int = 12):
    """运行一次 ReAct 循环，返回 (result, trace)。"""
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    trace = TraceRecorder(run_id, task)
    input_summary = {
        "topic": task.get("topic"),
        "audience": task.get("audience"),
        "platform": task.get("platform"),
        "target_duration": task.get("target_duration"),
        "style": task.get("style"),
    }
    history: list[dict] = []
    result = None

    for step in range(1, max_steps + 1):
        action = client.decide(task, history)
        if action.get("type") == "finish":
            payload = dict(action.get("payload") or {})
            payload.setdefault("status", "incomplete")
            payload.setdefault("message", "模型结束任务")
            payload["steps_used"] = step
            payload["step_limit"] = max_steps
            result = {"run_id": run_id, "input_summary": input_summary, **payload}
            trace.set_final(payload, step)
            break
        observation = registry.execute(action.get("tool"), action.get("args") or {})
        trace.add_step(step, action, observation)
        history.append({"action": action, "observation": observation})

    if result is None:
        payload = {
            "status": "incomplete",
            "message": f"达到执行步数上限 {max_steps}，任务未完成，已按边界终止",
            "steps_used": max_steps,
            "step_limit": max_steps,
        }
        result = {"run_id": run_id, "input_summary": input_summary, **payload}
        trace.set_final(payload, max_steps)

    return result, trace
