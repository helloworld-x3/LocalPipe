"""LocalPipe 自动化测试 — 用 unittest.mock 隔离 LLM，测真实业务规则"""
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import (
    _build_expected_checks,
    _evaluate_fidelity_checks,
    _make_cache_key,
    compute_recovery_rate,
)


# ========== 纯函数测试 ==========

class TestComputeRecoveryRate(unittest.TestCase):
    """保真率必须由程序计算，不信任模型自报值"""

    def test_all_false_model_reports_1_0(self):
        """业务规则：即使 LLM 返回 recovery_rate=1.0，
        只要 checks 全 false，程序计算值必须为 0.0"""
        checks = [
            {"element": "卖点A", "recovered": False},
            {"element": "卖点B", "recovered": False},
            {"element": "情绪钩子", "recovered": False},
        ]
        self.assertEqual(compute_recovery_rate(checks), 0.0)

    def test_empty_checks_returns_zero(self):
        """空 checks → 0.0，不抛异常"""
        self.assertEqual(compute_recovery_rate([]), 0.0)

    def test_none_checks_returns_zero(self):
        """checks=None → 0.0"""
        self.assertEqual(compute_recovery_rate(None), 0.0)

    def test_non_list_rejected(self):
        """非 list 输入 → 0.0"""
        self.assertEqual(compute_recovery_rate("not_a_list"), 0.0)
        self.assertEqual(compute_recovery_rate(42), 0.0)

    def test_missing_recovered_treated_as_false(self):
        """缺少 recovered 字段 → 按 false"""
        checks = [
            {"element": "A"},                          # 无 recovered
            {"element": "B", "recovered": True},
        ]
        self.assertEqual(compute_recovery_rate(checks), 0.5)

    def test_non_dict_entries_excluded(self):
        """非 dict 条目不计入总数也不计入回收"""
        checks = [
            {"element": "A", "recovered": True},
            "not_a_dict",
            {"element": "B", "recovered": False},
        ]
        self.assertEqual(compute_recovery_rate(checks), 0.5)

    def test_all_true_is_1_0(self):
        checks = [
            {"element": "A", "recovered": True},
            {"element": "B", "recovered": True},
        ]
        self.assertEqual(compute_recovery_rate(checks), 1.0)

    def test_string_false_is_not_recovered(self):
        self.assertEqual(
            compute_recovery_rate([{"element": "A", "recovered": "false"}]),
            0.0,
        )


class TestFidelityEvaluation(unittest.TestCase):
    def test_expected_checks_preserve_same_text_across_kinds(self):
        expected = _build_expected_checks({
            "selling_points": ["立即购买"],
            "emotion_hook": "别错过",
            "cta": "立即购买",
        })
        self.assertEqual(expected, [
            ("selling_point", "立即购买"),
            ("emotion_hook", "别错过"),
            ("cta", "立即购买"),
        ])

    def test_wrong_kind_does_not_match(self):
        expected = [("selling_point", "快")]
        evaluation = _evaluate_fidelity_checks(expected, [
            {"kind": "cta", "element": "快", "recovered": True},
        ])
        self.assertEqual(evaluation["rate"], 0.0)
        self.assertEqual(evaluation["failed"][0]["reason"], "missing")
        self.assertEqual(evaluation["unexpected"][0]["reason"], "unexpected")

    def test_duplicate_check_does_not_match(self):
        expected = [("selling_point", "快")]
        evaluation = _evaluate_fidelity_checks(expected, [
            {"kind": "selling_point", "element": "快", "recovered": True},
            {"kind": "selling_point", "element": "快", "recovered": True},
        ])
        self.assertEqual(evaluation["rate"], 0.0)
        self.assertEqual(evaluation["failed"][0]["reason"], "duplicate")

    def test_false_and_non_bool_have_distinct_reasons(self):
        expected = [("selling_point", "快"), ("cta", "买")]
        evaluation = _evaluate_fidelity_checks(expected, [
            {"kind": "selling_point", "element": "快", "recovered": False},
            {"kind": "cta", "element": "买", "recovered": "false"},
        ])
        self.assertEqual(evaluation["rate"], 0.0)
        self.assertEqual(
            [item["reason"] for item in evaluation["failed"]],
            ["not_recovered", "recovered_not_bool"],
        )
        self.assertFalse(evaluation["structure_valid"])

    def test_missing_check_invalidates_structure_even_above_threshold(self):
        expected = [
            ("selling_point", "A"),
            ("selling_point", "B"),
            ("emotion_hook", "C"),
            ("cta", "D"),
        ]
        evaluation = _evaluate_fidelity_checks(expected, [
            {"kind": kind, "element": element, "recovered": True}
            for kind, element in expected[:3]
        ])
        self.assertEqual(evaluation["rate"], 0.75)
        self.assertFalse(evaluation["structure_valid"])


class TestCacheKey(unittest.TestCase):
    """缓存键必须按厂商隔离"""

    def test_different_base_url_different_key(self):
        k1 = _make_cache_key("x", "m", 100, "https://api.deepseek.com")
        k2 = _make_cache_key("x", "m", 100, "https://api.dashscope.com")
        self.assertNotEqual(k1, k2)

    def test_same_params_same_key(self):
        k1 = _make_cache_key("x", "m", 100, "https://api.deepseek.com")
        k2 = _make_cache_key("x", "m", 100, "https://api.deepseek.com")
        self.assertEqual(k1, k2)

    def test_different_model_different_key(self):
        k1 = _make_cache_key("x", "v4-pro", 100, "https://api.deepseek.com")
        k2 = _make_cache_key("x", "v4-flash", 100, "https://api.deepseek.com")
        self.assertNotEqual(k1, k2)


# ========== 管线集成测试（打桩 LLM，验证端到端业务规则） ==========

class TestFidelityFinalStatus(unittest.TestCase):
    """保真率必须影响 final_status，程序重算值优先于模型自报"""

    def setUp(self):
        # 画像 mock：必须有足够条目让 used_entries 校验通过
        self.profile_patcher = patch(
            "pipeline.load_profile",
            return_value={
                "market": "泰国", "market_code": "th", "version": "v0.1",
                "language": "th",
                "entries": [
                    {"id": "th-001", "type": "流行梗", "confidence": 0.9, "content": ""},
                    {"id": "th-002", "type": "支付习惯", "confidence": 0.85, "content": ""},
                ],
                "_expired_ids": [],
            }
        )
        self.profile_patcher.start()

        self.hash_patcher = patch("pipeline.verify_profile_integrity")
        self.hash_patcher.start()

    def tearDown(self):
        self.profile_patcher.stop()
        self.hash_patcher.stop()

    def test_all_checks_false_model_says_1_0_cannot_pass(self):
        """业务规则：checks 全 false，即使 LLM 自报 recovery_rate=1.0，
        final_status 不能是 pass"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快", "便宜"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                # LLM 谎报 1.0，但 checks 全 false
                return {
                    "checks": [
                        {"element": "快", "kind": "selling_point", "recovered": False},
                        {"element": "便宜", "kind": "selling_point", "recovered": False},
                    ],
                    "recovery_rate": 1.0
                }
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertNotEqual(result["final_status"], "pass",
            "checks 全 false 时 final_status 不能为 pass")

    def test_checks_all_true_should_pass(self):
        """checks 全 true → 程序计算 1.0 → 可以 pass"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {
                    "checks": [
                        {"element": "快", "kind": "selling_point", "recovered": True},
                        {"element": "急", "kind": "emotion_hook", "recovered": True},
                        {"element": "买", "kind": "cta", "recovered": True},
                    ],
                    "recovery_rate": 0.0  # LLM 低报，程序应重算为 1.0
                }
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertEqual(result["final_status"], "pass",
            "checks 全 true 且 taboo clean 时应为 pass（程序重算覆盖 LLM 低报）")

    def test_empty_checks_blocks_pass(self):
        """checks=[] → 程序计算 0.0 → 不能 pass"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [], "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertNotEqual(result["final_status"], "pass")

    def test_incomplete_checks_count_as_failed(self):
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                    {"element": "快", "kind": "selling_point", "recovered": True},
                ], "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertEqual(result["final_status"], "needs_review")
        self.assertEqual(result["fidelity"]["recovery_rate"], 1 / 3)
        self.assertEqual(
            {item["reason"] for item in result["fidelity"]["_failed"]},
            {"missing"},
        )

    def test_false_check_is_in_retry_hint(self):
        from pipeline import localize

        recreate_inputs = []

        def fake_recreate(elements, profile, brand=None):
            recreate_inputs.append(deepcopy(elements))
            return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"],
                    "adaptation_note": "ok"}

        with patch("pipeline.deconstruct", return_value={
                "selling_points": ["快"], "emotion_hook": "急",
                "target_audience": "年轻人", "cta": "买",
             }), patch("pipeline.recreate", side_effect=fake_recreate), patch(
                "pipeline.fidelity_check", return_value={
                    "checks": [
                        {"element": "快", "kind": "selling_point", "recovered": False},
                        {"element": "急", "kind": "emotion_hook", "recovered": True},
                        {"element": "买", "kind": "cta", "recovered": True},
                    ],
                    "recovery_rate": 1.0,
                }
             ), patch("pipeline.taboo_check", return_value={"risk_level": "low", "flags": []}):
            result = localize("测试文案", "th", verbose=False)

        self.assertEqual(result["final_status"], "needs_review")
        self.assertEqual(len(recreate_inputs), 3)
        self.assertIn("selling_point:快(not_recovered)", recreate_inputs[1]["_retry_hint"])


class TestUsedEntriesFinalStatus(unittest.TestCase):
    """used_entries 校验必须影响 final_status"""

    def setUp(self):
        self.profile_patcher = patch(
            "pipeline.load_profile",
            return_value={
                "market": "泰国", "market_code": "th", "version": "v0.1",
                "language": "th",
                "entries": [
                    {"id": "th-001", "type": "流行梗", "confidence": 0.9, "content": ""},
                    {"id": "th-002", "type": "支付习惯", "confidence": 0.85, "content": ""},
                    {"id": "th-taboo", "type": "文化禁忌", "confidence": 0.95, "content": ""},
                ],
                "_expired_ids": [],
            }
        )
        self.profile_patcher.start()
        self.hash_patcher = patch("pipeline.verify_profile_integrity")
        self.hash_patcher.start()

    def tearDown(self):
        self.profile_patcher.stop()
        self.hash_patcher.stop()

    def test_invalid_id_blocks_pass(self):
        """used_entries 含不存在 ID → final_status != pass"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货",
                        "used_entries": ["th-001", "nonexistent-999"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                            {"element": "快", "kind": "selling_point", "recovered": True},
                            {"element": "急", "kind": "emotion_hook", "recovered": True},
                            {"element": "买", "kind": "cta", "recovered": True},
                        ],
                        "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertNotEqual(result["final_status"], "pass",
            "used_entries 含无效 ID 时不能 pass")
        self.assertIn("nonexistent-999", result["profile_trace"]["invalid_ids"])

    def test_taboo_id_blocks_pass(self):
        """used_entries 含文化禁忌 ID → final_status != pass"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货",
                        "used_entries": ["th-001", "th-taboo"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                            {"element": "快", "kind": "selling_point", "recovered": True},
                            {"element": "急", "kind": "emotion_hook", "recovered": True},
                            {"element": "买", "kind": "cta", "recovered": True},
                        ],
                        "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertNotEqual(result["final_status"], "pass",
            "used_entries 含禁忌 ID 时不能 pass")
        self.assertIn("th-taboo", result["profile_trace"]["taboo_ids"])

    def test_all_valid_can_pass(self):
        """used_entries 全部合法 + 保真通过 + 禁忌 clean → pass"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货",
                        "used_entries": ["th-001", "th-002"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                            {"element": "快", "kind": "selling_point", "recovered": True},
                            {"element": "急", "kind": "emotion_hook", "recovered": True},
                            {"element": "买", "kind": "cta", "recovered": True},
                        ],
                        "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertEqual(result["final_status"], "pass")

    def test_profile_trace_deduplicates(self):
        """used_entries 重复 → profile_trace 中去重"""
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货",
                        "used_entries": ["th-001", "th-001", "th-002"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                            {"element": "快", "kind": "selling_point", "recovered": True},
                            {"element": "急", "kind": "emotion_hook", "recovered": True},
                            {"element": "买", "kind": "cta", "recovered": True},
                        ],
                        "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertEqual(result["profile_trace"]["valid_ids"], ["th-001", "th-002"])
        self.assertEqual(result["used_entries"], ["th-001", "th-002"])

    def test_empty_used_entries_blocks_pass(self):
        from pipeline import localize

        def fake_llm(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": [],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                    {"element": "快", "kind": "selling_point", "recovered": True},
                    {"element": "急", "kind": "emotion_hook", "recovered": True},
                    {"element": "买", "kind": "cta", "recovered": True},
                ], "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}

        with patch("pipeline._llm_json", side_effect=fake_llm):
            result = localize("测试文案", "th", verbose=False)

        self.assertEqual(result["final_status"], "needs_review")
        self.assertTrue(result["profile_trace"]["empty_reference"])


# ========== A/B 盲测配对（直接调用 _process_one） ==========

class TestABBlindPairing(unittest.TestCase):
    """A/B 必须同时进入或同时剔除盲测集"""

    def test_both_ok_yields_two_items_same_pair(self):
        """A/B 均成功 → 恰好 2 条，组别 A+B，creative_id 相同"""
        from batch import _process_one

        creative = {"id": "C01", "text": "文案"}
        b_ok = {"copy": "B文案", "copy_zh": "", "final_status": "pass"}
        a_ok = {"copy": "A文案", "copy_zh": ""}

        with patch("batch.localize", return_value=b_ok), \
             patch("batch.localize_baseline", return_value=a_ok):
            results, blind, skipped = _process_one(creative, "th", None, True)

        self.assertEqual(len(blind), 2)
        groups = {i["_group"] for i in blind}
        creative_ids = {i["_creative_id"] for i in blind}
        markets = {i["market"] for i in blind}
        self.assertEqual(groups, {"A", "B"})
        self.assertEqual(creative_ids, {"C01"})
        self.assertEqual(markets, {"th"})
        self.assertEqual(len(skipped), 0)

    def test_b_error_a_ok_yields_empty_blind(self):
        """B final_status=error, A 成功 → blind_items 为空"""
        from batch import _process_one

        creative = {"id": "C01", "text": "文案"}
        b_err = {"copy": "", "final_status": "error"}

        with patch("batch.localize", return_value=b_err), \
             patch("batch.localize_baseline", return_value={"copy": "A文案"}):
            results, blind, skipped = _process_one(creative, "th", None, True)

        self.assertEqual(blind, [])
        self.assertEqual(len(skipped), 1)

    def test_a_empty_b_ok_yields_empty_blind(self):
        """A copy 为空, B 成功 → blind_items 为空"""
        from batch import _process_one

        creative = {"id": "C01", "text": "文案"}
        b_ok = {"copy": "B文案", "final_status": "pass"}

        with patch("batch.localize", return_value=b_ok), \
             patch("batch.localize_baseline", return_value={"copy": ""}):
            results, blind, skipped = _process_one(creative, "th", None, True)

        self.assertEqual(blind, [])
        self.assertEqual(len(skipped), 1)

    def test_b_needs_review_still_paired(self):
        """B needs_review（非 error）→ 仍应进盲测"""
        from batch import _process_one

        creative = {"id": "C01", "text": "文案"}
        b_review = {"copy": "B文案", "final_status": "needs_review"}

        with patch("batch.localize", return_value=b_review), \
             patch("batch.localize_baseline", return_value={"copy": "A文案"}):
            results, blind, skipped = _process_one(creative, "th", None, True)

        self.assertEqual(len(blind), 2)
        self.assertEqual(len(skipped), 0)

    def test_no_baseline_mode_b_ok_adds_single(self):
        """--no-baseline 模式 B 成功 → 仅 B 入盲测"""
        from batch import _process_one

        creative = {"id": "C01", "text": "文案"}
        b_ok = {"copy": "B文案", "final_status": "pass"}

        with patch("batch.localize", return_value=b_ok):
            results, blind, skipped = _process_one(creative, "th", None, False)

        self.assertEqual(len(blind), 1)
        self.assertEqual(blind[0]["_group"], "B")
        self.assertEqual(len(skipped), 0)


class TestBatchArtifacts(unittest.TestCase):
    def test_run_batch_writes_skipped_artifact_for_failed_pair(self):
        import batch

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            creatives_path = root / "creatives.json"
            creatives_path.write_text(
                json.dumps([{"id": "C01", "text": "文案"}], ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(batch, "BASE_DIR", str(root)), patch(
                "batch.load_brand_context", return_value=None
            ), patch(
                "batch.localize", return_value={"copy": "", "final_status": "error"}
            ), patch(
                "batch.localize_baseline", return_value={"copy": "A文案"}
            ), patch("batch.random.shuffle", side_effect=lambda items: None):
                paths = batch.run_batch(
                    str(creatives_path), ["th"], with_baseline=True, workers=1
                )

            self.assertEqual(set(paths), {"batch", "blind", "key", "skipped"})
            skipped = json.loads(Path(paths["skipped"]).read_text(encoding="utf-8"))
            blind = json.loads(Path(paths["blind"]).read_text(encoding="utf-8"))
            key = json.loads(Path(paths["key"]).read_text(encoding="utf-8"))
            self.assertEqual(skipped, [
                {"pair": "C01→th", "reason": "B:final_status=error"},
            ])
            self.assertEqual(blind, [])
            self.assertEqual(key, [])


if __name__ == "__main__":
    unittest.main()
