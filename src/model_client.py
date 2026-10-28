"""模型接入层：离线确定性 Mock 与 OpenAI 兼容 API 双入口。

边界声明（试卷要求标明）：
- Mock 模型以"任务 + 历史观察"为输入的策略函数模拟模型决策，只验证 ReAct
  流程与工具协议，不证明真实模型效果；它不是按场景硬编码的脚本——脚本创作、
  超时压缩、规则修正、无法满足时的缺口计算，都由同一策略根据观察结果推导，
  替换素材事实或目标时长后，脚本与动作会随之改变。
- API 模式走 OpenAI 兼容 /chat/completions（标准库 urllib 实现），与 Mock
  共用同一 ReAct 循环。本仓库交付时未配置真实 Key，该路径未经过真实模型验证。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS

# 脚本模板：事实句逐字嵌入素材原文（fact），模板措辞不含产品事实——编造在源头被阻断
_DRAFT = {
    "hook": (
        "还在犹豫怎么选吗？今天这期{topic}主题的视频，给大家带来话题度很高的{name}，"
        "我们一次性把它值得入手的理由讲清楚，记得看到最后。"
    ),
    "fact": (
        "{name}的核心亮点来了：{fact}。这一点在真实使用里相当实用，日常体验提升明显，"
        "你可以放心把它加入候选清单。"
    ),
    "cta": (
        "如果这期内容对你有帮助，别忘了点赞、关注加转发，把你的想法留在评论区，"
        "我们一起交流，下期节目不见不散。"
    ),
    "title": "《{topic}》完整版短视频脚本",
}
_COMPRESSED = {
    "hook": "{topic}到底怎么选？别纠结，{name}值得选的理由，一条视频讲清楚。",
    "fact": "先看这条硬理由：{fact}——就冲这一点，它在同类里已经很有诚意，日常用起来省心。",
    "cta": "觉得有用就点赞关注，把你的问题留在评论区，下期见。",
    "title": "《{topic}》精简版短视频脚本",
}
_SAFE_REPLACE = {"最": "相当", "第一": "靠前", "顶级": "出色", "国家级": "高标准"}


def _compose_script(task: dict, product: dict, compress: bool) -> tuple:
    """从素材事实组装脚本：返回 (title, scenes)。产品事实仅来自 facts 原文。"""
    tpl = _COMPRESSED if compress else _DRAFT
    topic = task.get("topic", "")
    name = product.get("name", "")
    scenes = [
        {"scene_no": 1, "type": "hook", "based_on": None, "narration": tpl["hook"].format(topic=topic, name=name)}
    ]
    no = 2
    for fact in product.get("facts", []):
        scenes.append(
            {
                "scene_no": no,
                "type": "fact",
                "based_on": product.get("product_id"),
                "narration": tpl["fact"].format(name=name, fact=fact),  # fact 逐字嵌入，不编造
            }
        )
        no += 1
    scenes.append({"scene_no": no, "type": "cta", "based_on": None, "narration": tpl["cta"]})
    return tpl["title"].format(topic=topic), scenes


def _correct_violations(scenes: list, title: str, violations: list) -> tuple:
    """按规则命中修正措辞（只动模板词，不动素材事实）。"""
    def fix(text: str) -> str:
        for v in violations:
            hit = v.get("hit_text", "")
            if hit and hit in text:
                text = text.replace(hit, _SAFE_REPLACE.get(hit, ""))
            if "%" in text and re.search(r"\d+\s*%", text):
                text = re.sub(r"[^，。]*\d+\s*%[^，。]*[，。]?", "", text)
        return text

    scenes = [
        {**s, "narration": fix(s["narration"])} for s in scenes
    ]
    return fix(title), scenes


def _last(history: list, tool: str):
    for h in reversed(history):
        if h["action"].get("tool") == tool:
            return h["observation"]
    return None


def _last_action(history: list, tool: str):
    for h in reversed(history):
        if h["action"].get("tool") == tool:
            return h["action"]
    return None


def _count(history: list, tool: str) -> int:
    return sum(1 for h in history if h["action"].get("tool") == tool)


def _steps_from_history(history: list) -> list:
    """步骤状态按实际观察记录：成功即 success，失败即 failed，重试成功保留两条记录。"""
    steps = []
    for h in history:
        obs = h["observation"]
        steps.append(
            {
                "tool": h["action"].get("tool"),
                "status": "success" if obs.get("status") in ("ok", "violations") else "failed",
            }
        )
    return steps


def _script_text(title: str, scenes: list) -> str:
    return title + "".join(s.get("narration", "") for s in scenes)


class MockModelClient:
    """离线确定性 Mock 模型（AGENT_MODEL=mock，默认）。"""

    def decide(self, task: dict, history: list) -> dict:
        topic = (task.get("topic") or "").strip()
        raw_target = task.get("target_duration")
        audience = task.get("audience") or "通用受众"
        platform = task.get("platform") or "通用平台"
        style = task.get("style") or "轻松实用"

        # 1) 缺关键信息（验收点：缺关键信息）——缺口由模型给出，不编造
        try:
            target = float(raw_target)
        except (TypeError, ValueError):
            target = None
        missing = []
        if not topic:
            missing.append("主题 topic")
        if target is None or target <= 0:
            missing.append("目标时长 target_duration（秒，正数）")
        if missing:
            return self._finish(
                "missing_input",
                f"缺少必填信息：{'、'.join(missing)}，无法开始创作",
                history,
                gaps=[f"缺口：{'、'.join(missing)}"],
                basis="任务缺少创作所需的关键信息，直接返回未完成与缺口",
            )

        # 2) 工具失败处理：重试一次（失败续跑），仍失败如实终止
        for tool in ("search_materials", "estimate_duration", "check_rules"):
            last = _last(history, tool)
            if last is not None and last.get("status") not in ("ok", "violations"):
                if _count(history, tool) < 2:
                    prev = _last_action(history, tool)
                    return {
                        "type": "tool_call",
                        "tool": tool,
                        "args": prev.get("args") or {},
                        "basis": f"{tool} 上次返回失败（{last.get('message')}），"
                        "按失败处理策略重试一次，保留已成功步骤",
                    }
                return self._finish(
                    "failed",
                    f"{tool} 重试后仍失败，任务终止",
                    history,
                    gaps=[f"{tool} 连续两次失败，暂无法完成创作；已成功步骤的结果已保留"],
                    basis="重试仍失败，按边界终止并如实报告",
                )

        # 3) 未检索：按主题检索素材
        search = _last(history, "search_materials")
        if search is None:
            return {
                "type": "tool_call",
                "tool": "search_materials",
                "args": {"query": topic},
                "basis": f"尚未检索主题「{topic}」的产品素材，先检索再创作（不凭空写产品事实）",
            }

        # 4) 检索无结果：模型决定再次检索确认；仍无则请求补充（不编造素材）
        if not search.get("results"):
            if _count(history, "search_materials") < 2:
                return {
                    "type": "tool_call",
                    "tool": "search_materials",
                    "args": {"query": topic},
                    "basis": "首次检索未命中，再次检索确认素材库是否收录该产品",
                }
            return self._finish(
                "no_material",
                f"素材库中未找到与「{topic}」相关的产品素材，请补充产品名称或提供素材",
                history,
                gaps=[f"素材库中没有「{topic}」的产品素材（已检索确认两次），需用户补充素材后才能创作"],
                basis="检索确认无素材，按边界请求补充，不编造产品事实",
            )

        product = search["results"][0]  # 使用相关度最高的产品素材
        est = _last(history, "estimate_duration")
        est_count = _count(history, "estimate_duration")

        # 5) 未估算：创作首稿（完整模板）并估算
        if est is None:
            title, scenes = _compose_script(task, product, compress=False)
            return {
                "type": "tool_call",
                "tool": "estimate_duration",
                "args": {"scenes": scenes},
                "basis": f"基于 {product['product_id']} 的素材事实创作首稿（{len(scenes)} 个分镜），"
                "调用时长估算获取事实依据（是否超时长由估算结果判断）",
            }

        # 6) 时长判断（模型决策，不是工具）：超时 -> 压缩重写；压缩仍超 -> 明确失败
        if est.get("total_sec", 0) > target:
            if est_count < 2:
                title, scenes = _compose_script(task, product, compress=True)
                return {
                    "type": "tool_call",
                    "tool": "estimate_duration",
                    "args": {"scenes": scenes},
                    "basis": f"观察到首稿估算 {est.get('total_sec')} 秒超出目标时长 {target:g} 秒，"
                    "压缩脚本（精简钩子与结尾、收紧事实句包装）并重新估算",
                }
            return self._finish(
                "cannot_meet",
                f"最精简版本估算仍为 {est.get('total_sec')} 秒，无法满足目标时长 {target:g} 秒",
                history,
                gaps=[
                    f"无法满足时长要求：压缩后最短可得约 {est.get('total_sec')} 秒，"
                    f"超出目标 {target:g} 秒约 {est.get('total_sec') - target:g} 秒，"
                    "不交付超时脚本"
                ],
                basis="压缩后仍超时长，按边界明确失败并给出缺口，不交付不达标脚本",
            )

        # 7) 时长达标：规则检查——每个版本交付前必须检查；命中即修正后复检
        check = _last(history, "check_rules")
        check_count = _count(history, "check_rules")
        last_est_action = _last_action(history, "estimate_duration")
        scenes = last_est_action.get("args", {}).get("scenes") or []
        title = (
            last_est_action.get("args", {}).get("title")
            or self._script_title(task, est_count)
        )
        current_text = _script_text(title, scenes)
        check_action = _last_action(history, "check_rules")
        checked_current = (
            check_action is not None
            and check_action.get("args", {}).get("full_text") == current_text
        )
        if not checked_current:
            # 当前版本还没有做过规则检查
            return {
                "type": "tool_call",
                "tool": "check_rules",
                "args": {"full_text": current_text},
                "basis": f"{est.get('total_sec')} 秒在目标 {target:g} 秒内，交付前对脚本全文做品牌规则检查",
            }
        if check.get("status") == "violations":
            if check_count >= 2:
                return self._finish(
                    "failed",
                    "规则检查修正后仍命中，无法交付合规脚本",
                    history,
                    gaps=["修正后仍命中品牌规则，需人工介入；不交付不合规脚本"],
                    basis="两次规则检查均未通过，按边界如实终止",
                )
            title, scenes = _correct_violations(scenes, title, check.get("violations", []))
            return {
                "type": "tool_call",
                "tool": "estimate_duration",
                "args": {"scenes": scenes, "title": title},
                "basis": f"规则检查命中 {len(check.get('violations', []))} 处，修正措辞后"
                "重新估算时长（修改后时长可能变化，必须重估）",
            }

        # 8) 时长与规则均通过：交付（仅交付脚本，不执行真实发布）
        breakdown = {b["scene_no"]: b["scene_sec"] for b in est.get("breakdown", [])}
        out_scenes = [
            {**s, "est_sec": breakdown.get(s.get("scene_no"))} for s in scenes
        ]
        risks = []
        for h in history:
            if h["action"].get("tool") == "check_rules" and h["observation"].get("status") == "violations":
                for v in h["observation"].get("violations", []):
                    risks.append(f"规则检查曾命中「{v.get('rule')}（{v.get('hit_text')}）」，已修正后复检通过")
        return self._finish(
            "delivered",
            "脚本已生成并通过时长与规则检查（仅交付脚本，不执行真实发布）",
            history,
            script={
                "title": title,
                "audience": audience,
                "platform": platform,
                "style": style,
                "scenes": out_scenes,
                "total_sec": est.get("total_sec"),
            },
            sources=[product["product_id"]],
            risks=risks,
            basis=f"最终版本估算 {est.get('total_sec')} 秒在目标 {target:g} 秒内且规则检查通过，交付脚本",
        )

    # ------------------------------------------------------------------

    def _script_title(self, task: dict, est_count: int) -> str:
        tpl = _COMPRESSED if est_count >= 2 else _DRAFT
        return tpl["title"].format(topic=task.get("topic", ""))

    def _finish(
        self,
        status: str,
        message: str,
        history: list,
        script=None,
        sources=None,
        risks=None,
        gaps=None,
        basis: str = "",
    ) -> dict:
        return {
            "type": "finish",
            "payload": {
                "status": status,
                "message": message,
                "script": script,
                "sources": sources or [],
                "risks": risks or [],
                "gaps": gaps or [],
                "steps": _steps_from_history(history),
            },
            "basis": basis,
        }


class ApiModelClient:
    """OpenAI 兼容 /chat/completions 客户端（标准库实现；未随本仓库验证）。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _chat(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def decide(self, task: dict, history: list) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"task": task, "tool_schemas": TOOL_SCHEMAS}, ensure_ascii=False
                ),
            },
        ]
        for i, h in enumerate(history):
            a = h["action"]
            messages.append(
                {
                    "role": "assistant",
                    "content": a.get("basis", ""),
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": a["tool"],
                                "arguments": json.dumps(
                                    a.get("args") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": json.dumps(h["observation"], ensure_ascii=False),
                }
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "description": s["description"],
                        "parameters": {"type": "object", "properties": s["params"]},
                    },
                }
                for s in TOOL_SCHEMAS
            ],
            "tool_choice": "auto",
        }
        resp = self._chat(payload)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if calls:
            fn = calls[0]["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            return {
                "type": "tool_call",
                "tool": fn["name"],
                "args": args,
                "basis": msg.get("content") or "模型发起工具调用",
            }
        content = msg.get("content") or ""
        try:
            payload_out = json.loads(content)
            if not isinstance(payload_out, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            payload_out = {"status": "incomplete", "message": content or "模型未返回可解析结果"}
        return {"type": "finish", "payload": payload_out, "basis": "模型直接给出最终结论"}


def build_client():
    """按 AGENT_MODEL 环境变量构建模型客户端，默认 mock。"""
    mode = os.environ.get("AGENT_MODEL", "mock").strip().lower()
    if mode == "mock":
        return MockModelClient()
    if mode == "api":
        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL")
        if not (base and key and model):
            raise RuntimeError(
                "AGENT_MODEL=api 需要设置 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL"
                "（参见 .env.example）"
            )
        return ApiModelClient(base, key, model)
    raise ValueError(f"未知 AGENT_MODEL: {mode}（可选 mock / api）")
