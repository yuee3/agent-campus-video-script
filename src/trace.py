"""Trace 记录（第三板斧：过程证据，用于评测、复现与审计）。

每步记录：步骤号、动作类型、工具名、工具参数、观察结果、决策依据。
不记录模型内部思维过程，符合试卷"无需提交模型内部思维过程"的要求。
"""
from __future__ import annotations

import datetime as _dt
import json
import os


class TraceRecorder:
    def __init__(self, run_id: str, task: dict) -> None:
        self.run_id = run_id
        self.started_at = _dt.datetime.now().isoformat(timespec="seconds")
        self.task = task
        self.steps: list[dict] = []
        self.final: dict | None = None

    def add_step(self, step: int, action: dict, observation: dict) -> None:
        self.steps.append(
            {
                "step": step,
                "action": action.get("type"),
                "tool": action.get("tool"),
                "tool_args": action.get("args"),
                "observation": observation,
                "decision_basis": action.get("basis", ""),
            }
        )

    def set_final(self, payload: dict, steps_used: int) -> None:
        self.final = {"steps_used": steps_used, **payload}

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "task": self.task,
            "steps": self.steps,
            "final": self.final,
        }


def write_trace(path: str, trace: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
