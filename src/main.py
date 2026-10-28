"""CLI 入口（试卷运行方式三选一：CLI）。

用法：
    python -m src.main --input examples/normal.input.json \
        [--data-dir data] [--trace-out examples/normal.trace.json] [--max-steps 12]

约定：程序成功输出 JSON；业务异常（缺信息/无素材/无法满足时长/工具失败/未完成）
以 JSON 状态返回且退出码 0；输入文件不是合法 JSON 时返回非零退出码。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .agent import run_agent
from .model_client import build_client
from .tools import ToolRegistry
from .trace import write_trace


def load_env(path: str = ".env") -> None:
    """极简 .env 加载：不覆盖已有环境变量。无此文件则跳过。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.main", description="短视频脚本 Agent（CLI）"
    )
    parser.add_argument("--input", required=True, help="任务输入 JSON 文件路径")
    parser.add_argument("--data-dir", default="data", help="合成素材与规则目录")
    parser.add_argument("--trace-out", default=None, help="Trace 输出 JSON 路径")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=int(os.environ.get("MAX_STEPS", "12")),
        help="ReAct 循环步数上限",
    )
    args = parser.parse_args(argv)
    load_env()

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            task = json.load(f)
    except Exception as e:  # noqa: BLE001 - 输入边界校验
        print(
            json.dumps(
                {"status": "invalid_input", "message": f"输入文件不可读或不是合法 JSON: {e}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    if not isinstance(task, dict):
        print(
            json.dumps(
                {"status": "invalid_input", "message": "输入必须是 JSON 对象"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        client = build_client()
    except (RuntimeError, ValueError) as e:
        print(
            json.dumps({"status": "config_error", "message": str(e)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    registry = ToolRegistry(args.data_dir, inject=task.get("inject") or {})
    result, trace = run_agent(task, client, registry, max_steps=args.max_steps)

    if args.trace_out:
        write_trace(args.trace_out, trace.to_dict())
        result["trace_file"] = args.trace_out

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
