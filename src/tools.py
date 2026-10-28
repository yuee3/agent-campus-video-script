"""业务工具层（第一板斧：外部能力入口与边界）。

设计边界：
- Tool 只执行原子动作，不承载完整业务流程；流程编排由 agent.py 的 ReAct 循环完成。
- 题目指定三类工具：素材检索（search_materials）、时长估算（estimate_duration）、
  规则检查（check_rules），均为只读；脚本生成与修改是模型行为（脚本作为工具入参）。
- 工具只返回事实：estimate_duration 只按"旁白字数/语速 + 分镜固定开销"给总时长与
  分镜明细，**不判断是否超目标时长**；check_rules 只报规则命中，**不判断如何改**。
  "估算 vs 目标"的对比、压缩重写、修正措辞，全部由模型完成——修正才是真实的动态决策。
- 素材检索按 2-gram 重合度打分（模拟检索引擎容错的有限性）；单次故障注入在工具层，
  由输入显式声明，可确定性复现。
"""
from __future__ import annotations

import json
import math
import os
import re

DATA_FILES = {"products": "products.json", "rules": "brand_rules.json"}

TOOL_SCHEMAS = [
    {
        "name": "search_materials",
        "description": "按关键词检索产品素材库，返回产品名称与产品事实清单（只读）",
        "params": {"query": "string，必填，产品/主题关键词"},
    },
    {
        "name": "estimate_duration",
        "description": (
            "按旁白语速与分镜固定开销估算脚本总时长，返回 total_sec 与逐分镜 breakdown"
            "（只读；只给事实，不判断是否超目标时长）"
        ),
        "params": {
            "scenes": "array，必填，每项含 scene_no 与 narration（旁白/字幕文本）",
        },
    },
    {
        "name": "check_rules",
        "description": "对脚本全文做品牌规则检查（绝对化用语/无数据支撑的百分比结论等），返回命中明细（只读）",
        "params": {"full_text": "string，必填，脚本标题与全部旁白拼接文本"},
    },
]


def _read(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _grams(text: str) -> set:
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i: i + 2] for i in range(len(cleaned) - 1)}


class ToolRegistry:
    """工具注册表：模型通过名称调用；执行、参数校验、故障注入与审计均在本层完成。"""

    def __init__(self, data_dir: str, inject: dict | None = None) -> None:
        self.data_dir = data_dir
        self.inject = inject or {}
        self._search_calls = 0  # 用于实现"注入一次失败"

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, DATA_FILES[name])

    # ------------------------------------------------------------------
    # 工具实现（每个工具返回一个 JSON 观察结果，异常不外抛）
    # ------------------------------------------------------------------

    def search_materials(self, query: str) -> dict:
        # 注入一次失败（工具失败验收场景）：首次调用失败，重试成功
        if self.inject.get("search_fail_once") and self._search_calls == 0:
            self._search_calls += 1
            return {
                "status": "failed",
                "query": query,
                "error": "transient",
                "message": "素材检索失败：素材服务临时不可用（注入的单次故障）",
            }
        self._search_calls += 1
        products = _read(self._path("products")).get("products", {})
        q_grams = _grams(query)
        results = []
        for pid, p in products.items():
            hay = " ".join([p.get("name", ""), p.get("category", "")] + p.get("facts", []))
            hits = sum(1 for g in q_grams if g in hay)
            name_hits = sum(1 for g in q_grams if g in p.get("name", ""))
            score = hits + 2 * name_hits
            if score > 0:
                results.append(
                    {
                        "product_id": pid,
                        "name": p.get("name"),
                        "category": p.get("category"),
                        "facts": p.get("facts", []),
                        "score": score,
                    }
                )
        results.sort(key=lambda r: (-r["score"], r["product_id"]))
        return {"status": "ok", "query": query, "results": results[:2]}

    def estimate_duration(self, scenes: list) -> dict:
        if not isinstance(scenes, list) or not scenes:
            return {
                "status": "invalid_args",
                "message": "scenes 必须是非空数组，每项含 scene_no 与 narration",
            }
        rules = _read(self._path("rules"))
        speed = rules.get("narration_speed_chars_per_sec", 4)
        overhead = rules.get("shot_overhead_sec", 1)
        breakdown = []
        total = 0
        for i, scene in enumerate(scenes, start=1):
            narration = str((scene or {}).get("narration", "") or "")
            chars = len(narration.replace(" ", ""))
            speak = math.ceil(chars / speed) if chars else 0
            scene_sec = speak + overhead
            total += scene_sec
            breakdown.append(
                {
                    "scene_no": (scene or {}).get("scene_no", i),
                    "narration_chars": chars,
                    "speed_chars_per_sec": speed,
                    "speak_sec": speak,
                    "overhead_sec": overhead,
                    "scene_sec": scene_sec,
                }
            )
        return {
            "status": "ok",
            "total_sec": total,
            "breakdown": breakdown,
            "note": "估算依据：旁白字数/语速 + 每分镜固定开销；是否超目标时长由调用方判断",
        }

    def check_rules(self, full_text: str) -> dict:
        if not full_text or not str(full_text).strip():
            return {"status": "invalid_args", "message": "full_text 不能为空"}
        text = str(full_text)
        rules = _read(self._path("rules"))
        violations = []
        for item in rules.get("banned_terms", []):
            if item["term"] in text:
                violations.append({"rule": item["rule"], "hit_text": item["term"]})
        for item in rules.get("unsupported_patterns", []):
            m = re.search(item["pattern"], text)
            if m:
                violations.append(
                    {"rule": item["rule"], "hit_text": m.group(0)}
                )
        return {
            "status": "violations" if violations else "ok",
            "violations": violations,
            "rules_checked": [i["rule"] for i in rules.get("banned_terms", [])]
            + [i["rule"] for i in rules.get("unsupported_patterns", [])],
            "note": "命中即为事实；是否修改脚本、如何修改由调用方决策",
        }

    # ------------------------------------------------------------------
    # 分发与审计入口
    # ------------------------------------------------------------------

    def execute(self, name: str, args: dict) -> dict:
        """按名称执行工具。任何异常都转为观察结果交回模型（错误恢复），不终止循环。"""
        try:
            if name == "search_materials":
                return self.search_materials(args["query"])
            if name == "estimate_duration":
                return self.estimate_duration(args["scenes"])
            if name == "check_rules":
                return self.check_rules(args["full_text"])
            return {"status": "unknown_tool", "message": f"未知工具：{name}"}
        except KeyError as e:
            return {"status": "invalid_args", "message": f"工具 {name} 缺少必填参数: {e}"}
        except Exception as e:  # noqa: BLE001 - 观察结果化是 Loop 错误恢复的一部分
            return {"status": "tool_error", "message": f"工具 {name} 执行异常: {e}"}
