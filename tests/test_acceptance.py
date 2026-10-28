"""验收测试：题目四「短视频脚本 Agent」的必须验收点 + 红线。

运行：python -m unittest discover -s tests -v

覆盖：正常生成 / 缺关键信息 / 无素材 / 工具失败（续跑）/
超时长后的修正（参考实例：P01 30 秒 -> 首稿超时 -> 压缩 -> 交付）或明确失败 /
步数上限 / 改变时长或素材后动作随之调整（审核要点鲁棒性）+ CLI 边界。
红线（对应试卷审核要点）在 assert_red_lines 统一断言：
1. 事实可追溯：delivered 时产品事实分镜的旁白逐字包含素材 facts 原文，
   sources 与实际检索到的素材 ID 对应，分镜 est_sec 与估算工具 breakdown 一致；
2. 不编造：全脚本不含"效率提升 X%"等无数据支撑的百分比结论；
3. 时长可靠：delivered 时 total_sec 来自估算工具且 <= 目标时长，不交付超时脚本；
4. 失败步骤不得记为成功；输入原样回显。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from src.agent import run_agent  # noqa: E402
from src.model_client import MockModelClient  # noqa: E402
from src.tools import ToolRegistry  # noqa: E402


def make_data_dir(tmp_dir: str) -> str:
    data_dir = os.path.join(tmp_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    for name in ("products.json", "brand_rules.json"):
        shutil.copy(os.path.join(REPO_ROOT, "data", name), os.path.join(data_dir, name))
    return data_dir


def load_products(data_dir: str) -> dict:
    with open(os.path.join(data_dir, "products.json"), encoding="utf-8") as f:
        return json.load(f)["products"]


def run_task(task: dict, data_dir: str, max_steps: int = 12):
    registry = ToolRegistry(data_dir, inject=task.get("inject") or {})
    return run_agent(task, MockModelClient(), registry, max_steps=max_steps)


def base_task(**kw) -> dict:
    task = {
        "topic": "便携咖啡杯",
        "audience": "大学生",
        "platform": "抖音",
        "target_duration": 90,
        "style": "轻松种草",
    }
    task.update(kw)
    return task


def assert_red_lines(test: unittest.TestCase, result: dict, task: dict, data_dir: str) -> None:
    products = load_products(data_dir)
    status = result["status"]
    # 红线 1+2+3：交付脚本必须事实可追溯、不编造、时长达标
    if status == "delivered":
        script = result["script"]
        test.assertLessEqual(script["total_sec"], float(task["target_duration"]))
        # 素材来源与检索结果对应，且分镜中的产品事实逐字来自素材原文
        test.assertTrue(result["sources"])
        for pid in result["sources"]:
            test.assertIn(pid, products)
        used_facts = [f for p in products.values() if p["name"] for f in p["facts"]]
        pid = result["sources"][0]
        fact_scenes = [s for s in script["scenes"] if s.get("type") == "fact"]
        test.assertTrue(fact_scenes)
        for s in fact_scenes:
            test.assertEqual(s.get("based_on"), pid)
            test.assertTrue(
                any(f in s["narration"] for f in products[pid]["facts"]),
                f"事实分镜旁白未逐字引用素材事实: {s['narration']}",
            )
        # 分镜时长与估算工具结果逐一对应
        est_sum = sum(s["est_sec"] for s in script["scenes"])
        test.assertEqual(est_sum, script["total_sec"])
        # 不编造：无百分比结论
        full_text = script["title"] + "".join(s["narration"] for s in script["scenes"])
        test.assertIsNone(re.search(r"\d+\s*%", full_text), "脚本中不应出现无数据支撑的百分比结论")
    # 非交付状态必须说明缺口且不交出脚本
    if status in ("missing_input", "no_material", "cannot_meet", "failed", "incomplete"):
        test.assertIsNone(result["script"])
        test.assertTrue(result["gaps"])
    for step in result["steps"]:
        test.assertIn(step["status"], ("success", "failed"))
    test.assertEqual(result["input_summary"]["topic"], task.get("topic"))
    test.assertEqual(result["input_summary"]["target_duration"], task.get("target_duration"))


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = make_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_1_normal_delivered(self):
        """验收点：正常生成——检索 -> 首稿在时长内 -> 规则检查通过 -> 交付。"""
        task = base_task()
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["sources"], ["P02"])
        tools = [(s["tool"], s["status"]) for s in result["steps"]]
        self.assertEqual(
            tools,
            [
                ("search_materials", "success"),
                ("estimate_duration", "success"),
                ("check_rules", "success"),
            ],
        )
        self.assertGreater(len(result["script"]["scenes"]), 3)

    def test_2_over_duration_compress_and_deliver(self):
        """验收点（参考实例）：P01 仅含"支持字幕"事实，目标 30 秒——
        首稿估算超时 -> 压缩脚本并重新估算 -> 交付含分镜、P01 引用及总时长。"""
        task = base_task(topic="学习平板", target_duration=30)
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["sources"], ["P01"])
        est_steps = [s for s in result["steps"] if s["tool"] == "estimate_duration"]
        self.assertEqual(len(est_steps), 2)  # 首稿 + 压缩稿各估一次
        # 决策依据写明压缩原因（超时 -> 修正 -> 复查）
        est2 = next(
            s
            for s in trace.to_dict()["steps"]
            if s["tool"] == "estimate_duration" and s["step"] > 2
        )
        self.assertIn("压缩", est2["decision_basis"])
        # 事实句逐字引用 P01 的"支持字幕"
        fact_scene = next(s for s in result["script"]["scenes"] if s["type"] == "fact")
        self.assertIn("支持字幕", fact_scene["narration"])
        self.assertEqual(fact_scene["based_on"], "P01")

    def test_3_cannot_meet_explicit_failure(self):
        """验收点：超时长且压缩后仍不达标 -> 明确失败并说明缺口，不交付超时脚本。"""
        task = base_task(topic="学习平板", target_duration=10)
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "cannot_meet")
        self.assertIsNone(result["script"])
        self.assertIn("超出目标", result["gaps"][0])
        est_steps = [s for s in result["steps"] if s["tool"] == "estimate_duration"]
        self.assertEqual(len(est_steps), 2)

    def test_4_missing_info(self):
        """验收点：缺关键信息（无目标时长）-> 返回缺口，不调用工具。"""
        task = base_task(target_duration=None)
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "missing_input")
        self.assertEqual(trace.to_dict()["steps"], [])
        self.assertIn("target_duration", result["gaps"][0])

    def test_5_no_material_ask_for_more(self):
        """验收点：无素材 -> 两轮检索确认 -> 请求补充，不编造产品事实。"""
        task = base_task(topic="校园无人机", target_duration=60)
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "no_material")
        search_calls = [s for s in result["steps"] if s["tool"] == "search_materials"]
        self.assertEqual(len(search_calls), 2)
        self.assertIn("补充", result["message"])

    def test_6_tool_failure_retry(self):
        """验收点：工具失败——注入一次检索失败，重试成功后正常交付；
        失败步骤保留 failed 记录。"""
        task = base_task(inject={"search_fail_once": True})
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "delivered")
        tools = [(s["tool"], s["status"]) for s in result["steps"]]
        self.assertEqual(
            tools,
            [
                ("search_materials", "failed"),
                ("search_materials", "success"),
                ("estimate_duration", "success"),
                ("check_rules", "success"),
            ],
        )

    def test_7_no_fabricated_claims(self):
        """红线（参考实例）：无效数据时不得添加"效率提升 80%"等结论——
        素材 P01 只有"支持字幕"一条事实，交付脚本不得出现任何百分比断言。"""
        task = base_task(topic="学习平板", target_duration=60)
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "delivered")
        full_text = json.dumps(result["script"], ensure_ascii=False)
        self.assertIsNone(re.search(r"\d+\s*%", full_text))
        self.assertNotIn("效率提升", full_text)

    def test_8_duration_change_alters_action(self):
        """审核要点鲁棒性：改变时长，核对下一步动作是否调整——
        同一素材（P01）目标 30 秒 -> 压缩后交付；目标 10 秒 -> 明确失败。"""
        result30, _ = run_task(base_task(topic="学习平板", target_duration=30), self.data_dir)
        self.assertEqual(result30["status"], "delivered")
        result10, _ = run_task(base_task(topic="学习平板", target_duration=10), self.data_dir)
        self.assertEqual(result10["status"], "cannot_meet")
        # 改变素材事实，脚本内容随之改变（事实来自素材而非硬编码）
        with open(os.path.join(self.data_dir, "products.json"), encoding="utf-8") as f:
            products = json.load(f)
        products["products"]["P01"]["facts"] = ["支持字幕", "护眼屏幕"]
        with open(os.path.join(self.data_dir, "products.json"), "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        result_facts, _ = run_task(base_task(topic="学习平板", target_duration=60), self.data_dir)
        assert_red_lines(self, result_facts, {"topic": "学习平板", "target_duration": 60}, self.data_dir)
        fact_narrations = [
            s["narration"] for s in result_facts["script"]["scenes"] if s["type"] == "fact"
        ]
        self.assertEqual(len(fact_narrations), 2)
        self.assertTrue(any("护眼屏幕" in n for n in fact_narrations))

    def test_9_max_steps_incomplete(self):
        """边界：达到步数上限时返回明确的未完成状态。"""
        task = base_task()
        result, _ = run_task(task, self.data_dir, max_steps=3)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("上限", result["message"])
        self.assertEqual(result["steps_used"], 3)


class CliTests(unittest.TestCase):
    def test_cli_normal_exit_zero_and_trace(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            data_dir = make_data_dir(tmp.name)
            trace_out = os.path.join(tmp.name, "out.trace.json")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--input",
                    os.path.join(REPO_ROOT, "examples", "normal.input.json"),
                    "--data-dir",
                    data_dir,
                    "--trace-out",
                    trace_out,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "delivered")
            with open(trace_out, encoding="utf-8") as f:
                trace = json.load(f)
            self.assertTrue(trace["steps"])
        finally:
            tmp.cleanup()

    def test_cli_invalid_input_nonzero(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            bad_json = os.path.join(tmp.name, "bad.json")
            with open(bad_json, "w", encoding="utf-8") as f:
                f.write("{not json")
            proc = subprocess.run(
                [sys.executable, "-m", "src.main", "--input", bad_json],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
