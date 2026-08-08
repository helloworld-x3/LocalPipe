"""LocalPipe 行为测试：隔离 LLM，验证核心规则和业务输出契约。"""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from pipeline import (
    _build_expected_checks,
    _evaluate_fidelity_checks,
    _make_cache_key,
    compute_recovery_rate,
)


class TestCorePureRules(unittest.TestCase):
    def test_recovery_rate_ignores_model_claim(self):
        self.assertEqual(compute_recovery_rate([
            {"recovered": False}, {"recovered": True}, {"recovered": "true"}
        ]), 1 / 3)

    def test_recovery_rate_handles_invalid_inputs(self):
        self.assertEqual(compute_recovery_rate([]), 0.0)
        self.assertEqual(compute_recovery_rate(None), 0.0)
        self.assertEqual(compute_recovery_rate("bad"), 0.0)

    def test_recovery_rate_missing_field_counts_as_false(self):
        self.assertEqual(compute_recovery_rate([{}, {"recovered": True}]), 0.5)

    def test_recovery_rate_ignores_non_dict_entries(self):
        self.assertEqual(compute_recovery_rate([{"recovered": True}, "bad"]), 1.0)

    def test_recovery_rate_all_true_is_one(self):
        self.assertEqual(compute_recovery_rate([{"recovered": True}]), 1.0)

    def test_fidelity_requires_kind_and_unique_expected_items(self):
        expected = [("selling_point", "快"), ("cta", "买")]
        result = _evaluate_fidelity_checks(expected, [
            {"kind": "cta", "element": "快", "recovered": True},
            {"kind": "selling_point", "element": "快", "recovered": True},
            {"kind": "selling_point", "element": "快", "recovered": True},
        ])
        self.assertEqual(result["rate"], 0.0)
        self.assertFalse(result["structure_valid"])
        self.assertEqual(result["failed"][0]["reason"], "duplicate")

    def test_fidelity_distinguishes_false_from_non_bool(self):
        result = _evaluate_fidelity_checks(
            [("selling_point", "快"), ("cta", "买")],
            [{"kind": "selling_point", "element": "快", "recovered": False},
             {"kind": "cta", "element": "买", "recovered": "false"}],
        )
        self.assertEqual([x["reason"] for x in result["failed"]], ["not_recovered", "recovered_not_bool"])

    def test_fidelity_missing_item_invalidates_structure(self):
        result = _evaluate_fidelity_checks(
            [("selling_point", "A"), ("selling_point", "B")],
            [{"kind": "selling_point", "element": "A", "recovered": True}],
        )
        self.assertEqual(result["rate"], 0.5)
        self.assertFalse(result["structure_valid"])

    def test_fidelity_cultural_alignment_false_opens_gate(self):
        result = _evaluate_fidelity_checks(
            [("selling_point", "快")],
            [{"kind": "selling_point", "element": "快", "recovered": True},
             {"kind": "cultural_alignment", "element": "整体文化对齐", "recovered": False}],
        )
        self.assertEqual(result["rate"], 1.0)
        self.assertTrue(result["structure_valid"])
        self.assertTrue(result["alignment_checked"])
        self.assertTrue(result["alignment_failed"])

    def test_fidelity_cultural_alignment_true_passes_gate(self):
        result = _evaluate_fidelity_checks(
            [("selling_point", "快")],
            [{"kind": "selling_point", "element": "快", "recovered": True},
             {"kind": "cultural_alignment", "element": "整体文化对齐", "recovered": True}],
        )
        self.assertEqual(result["rate"], 1.0)
        self.assertTrue(result["alignment_checked"])
        self.assertFalse(result["alignment_failed"])

    def test_weighted_rate_treats_protected_term_as_critical(self):
        expected = [("selling_point", "快"), ("protected_term", "雾川"), ("emotion_hook", "舒服")]
        lost_protected = _evaluate_fidelity_checks(
            expected,
            [{"kind": "selling_point", "element": "快", "recovered": True},
             {"kind": "emotion_hook", "element": "舒服", "recovered": True}],
        )
        lost_emotion = _evaluate_fidelity_checks(
            expected,
            [{"kind": "selling_point", "element": "快", "recovered": True},
             {"kind": "protected_term", "element": "雾川", "recovered": True}],
        )
        # 丢品牌词(3) 比丢情绪词(1) 扣分重：3/6=0.5 < (2+3)/6≈0.833
        self.assertLess(lost_protected["rate"], lost_emotion["rate"])
        self.assertAlmostEqual(lost_protected["rate"], 0.5)
        self.assertAlmostEqual(lost_emotion["rate"], 5 / 6)

    def test_weighted_rate_numeric_selling_point_is_critical(self):
        expected = [("selling_point", "3秒降温15度"), ("selling_point", "便携")]
        result = _evaluate_fidelity_checks(
            expected,
            [{"kind": "selling_point", "element": "便携", "recovered": True}],
        )
        # 数字事实(权重3)丢失、普通卖点(权重2)保留 → 2/(3+2)=0.4；简单平均仍是 1/2
        self.assertAlmostEqual(result["rate"], 0.4)
        self.assertAlmostEqual(result["rate_unweighted"], 0.5)

    def test_weighted_rate_exposes_unweighted_comparison(self):
        expected = [("selling_point", "快"), ("emotion_hook", "舒服"), ("cta", "买")]
        result = _evaluate_fidelity_checks(
            expected,
            [{"kind": "selling_point", "element": "快", "recovered": True},
             {"kind": "cta", "element": "买", "recovered": True}],
        )
        # 简单平均 2/3；加权 (2+1)/(2+1+1)=3/4
        self.assertAlmostEqual(result["rate_unweighted"], 2 / 3)
        self.assertAlmostEqual(result["rate"], 0.75)

    def test_expected_checks_keep_same_text_when_kind_differs(self):
        self.assertEqual(_build_expected_checks({
            "selling_points": ["立即购买"],
            "emotion_hook": "别错过",
            "cta": "立即购买",
        }), [
            ("selling_point", "立即购买"),
            ("emotion_hook", "别错过"),
            ("cta", "立即购买"),
        ])

    def test_cache_key_separates_model_provider(self):
        self.assertNotEqual(
            _make_cache_key("x", "m", 1, "https://a"),
            _make_cache_key("x", "m", 1, "https://b"),
        )

    def test_cache_key_is_stable_for_same_inputs(self):
        self.assertEqual(_make_cache_key("x", "m", 1, "https://a"), _make_cache_key("x", "m", 1, "https://a"))

    def test_cache_key_separates_models(self):
        self.assertNotEqual(_make_cache_key("x", "m1", 1, "https://a"), _make_cache_key("x", "m2", 1, "https://a"))


class TestPipelineFinalStatus(unittest.TestCase):
    def setUp(self):
        self.profile_data = {
            "market": "泰国", "market_code": "th", "version": "v0.1",
            "language": "th", "entries": [
                {"id": "th-001", "type": "流行梗", "confidence": 0.9, "content": ""},
                {"id": "th-002", "type": "支付习惯", "confidence": 0.8, "content": ""},
            ], "_expired_ids": [],
        }
        self.profile = patch("pipeline.load_profile", return_value=self.profile_data)
        self.profile.start()
        self.integrity = patch("pipeline.verify_profile_integrity")
        self.integrity.start()

    def tearDown(self):
        self.profile.stop()
        self.integrity.stop()

    def _llm(self, all_true=True, used=None):
        def fake(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急",
                        "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货",
                        "used_entries": used if used is not None else ["th-001"],
                        "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [
                    {"kind": "selling_point", "element": "快", "recovered": all_true},
                    {"kind": "emotion_hook", "element": "急", "recovered": all_true},
                    {"kind": "cta", "element": "买", "recovered": all_true},
                ], "recovery_rate": 1.0}
            if schema == "taboo":
                return {"risk_level": "low", "flags": []}
            return {}
        return fake

    def test_all_true_passes(self):
        with patch("pipeline._llm_json", side_effect=self._llm()):
            from pipeline import localize
            self.assertEqual(localize("测试文案", "th", verbose=False)["final_status"], "pass")

    def test_all_false_is_not_pass_even_if_model_claims_one(self):
        with patch("pipeline._llm_json", side_effect=self._llm(all_true=False)):
            from pipeline import localize
            self.assertNotEqual(localize("测试文案", "th", verbose=False)["final_status"], "pass")

    def test_invalid_or_empty_profile_reference_needs_review(self):
        from pipeline import localize
        for used in (["missing"], []):
            with self.subTest(used=used), patch("pipeline._llm_json", side_effect=self._llm(used=used)):
                self.assertEqual(localize("测试文案", "th", verbose=False)["final_status"], "needs_review")

    def test_empty_profile_reference_needs_review(self):
        from pipeline import localize
        with patch("pipeline._llm_json", side_effect=self._llm(used=[])):
            result = localize("测试文案", "th", verbose=False)
        self.assertTrue(result["profile_trace"]["empty_reference"])
        self.assertEqual(result["final_status"], "needs_review")

    def test_incomplete_fidelity_needs_review(self):
        from pipeline import localize
        def fake(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急", "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"], "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [{"kind": "selling_point", "element": "快", "recovered": True}], "recovery_rate": 1.0}
            return {"risk_level": "low", "flags": []} if schema == "taboo" else {}
        with patch("pipeline._llm_json", side_effect=fake):
            result = localize("测试文案", "th", verbose=False)
        self.assertEqual(result["final_status"], "needs_review")

    def test_fidelity_failed_item_is_added_to_retry_hint(self):
        from pipeline import localize
        recreates = []
        def fake_recreate(elements, profile, brand=None):
            recreates.append(elements.copy())
            return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001"], "adaptation_note": "ok"}
        with patch("pipeline.deconstruct", return_value={"selling_points": ["快"], "emotion_hook": "急", "target_audience": "年轻人", "cta": "买"}), \
             patch("pipeline.recreate", side_effect=fake_recreate), \
             patch("pipeline.fidelity_check", return_value={"checks": [
                 {"kind": "selling_point", "element": "快", "recovered": False},
                 {"kind": "emotion_hook", "element": "急", "recovered": True},
                 {"kind": "cta", "element": "买", "recovered": True}], "recovery_rate": 1.0}), \
             patch("pipeline.taboo_check", return_value={"risk_level": "low", "flags": []}):
            result = localize("测试文案", "th", verbose=False)
        self.assertEqual(result["final_status"], "needs_review")
        self.assertIn("selling_point:快(not_recovered)", recreates[1]["_retry_hint"])

    def test_taboo_profile_reference_needs_review(self):
        from pipeline import localize
        self.profile_data["entries"].append({"id": "th-taboo", "type": "文化禁忌", "confidence": 0.9, "content": ""})
        def fake(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急", "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-taboo"], "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [{"kind": k, "element": e, "recovered": True} for k, e in [("selling_point", "快"), ("emotion_hook", "急"), ("cta", "买")]], "recovery_rate": 1.0}
            return {"risk_level": "low", "flags": []} if schema == "taboo" else {}
        with patch("pipeline._llm_json", side_effect=fake):
            result = localize("测试文案", "th", verbose=False)
        self.assertEqual(result["final_status"], "needs_review")

    def test_used_entries_are_deduplicated(self):
        from pipeline import localize
        def fake(prompt, max_tokens=900, schema=None):
            if schema == "deconstruct":
                return {"selling_points": ["快"], "emotion_hook": "急", "target_audience": "年轻人", "cta": "买"}
            if schema == "recreate":
                return {"copy": "好货", "copy_zh": "好货", "used_entries": ["th-001", "th-001"], "adaptation_note": "ok"}
            if schema == "fidelity":
                return {"checks": [{"kind": k, "element": e, "recovered": True} for k, e in [("selling_point", "快"), ("emotion_hook", "急"), ("cta", "买")]], "recovery_rate": 1.0}
            return {"risk_level": "low", "flags": []} if schema == "taboo" else {}
        with patch("pipeline._llm_json", side_effect=fake):
            result = localize("测试文案", "th", verbose=False)
        self.assertEqual(result["used_entries"], ["th-001"])


class TestABPairing(unittest.TestCase):
    def test_failed_side_skips_whole_pair(self):
        from batch import _process_one
        with patch("batch.localize", return_value={"copy": "", "final_status": "error"}), \
             patch("batch.localize_baseline", return_value={"copy": "A"}):
            _, blind, skipped = _process_one({"id": "C01", "text": "文案"}, "th", None, True)
        self.assertEqual(blind, [])
        self.assertEqual(len(skipped), 1)

    def test_needs_review_with_copy_still_pairs(self):
        from batch import _process_one
        with patch("batch.localize", return_value={"copy": "B", "final_status": "needs_review"}), \
             patch("batch.localize_baseline", return_value={"copy": "A"}):
            _, blind, skipped = _process_one({"id": "C01", "text": "文案"}, "th", None, True)
        self.assertEqual(len(blind), 2)
        self.assertEqual(skipped, [])

    def test_empty_baseline_skips_pair(self):
        from batch import _process_one
        with patch("batch.localize", return_value={"copy": "B", "final_status": "pass"}), \
             patch("batch.localize_baseline", return_value={"copy": ""}):
            _, blind, skipped = _process_one({"id": "C01", "text": "文案"}, "th", None, True)
        self.assertEqual(blind, [])
        self.assertEqual(len(skipped), 1)

    def test_no_baseline_mode_returns_pipeline_item(self):
        from batch import _process_one
        with patch("batch.localize", return_value={"copy": "B", "final_status": "pass"}):
            _, blind, skipped = _process_one({"id": "C01", "text": "文案"}, "th", None, False)
        self.assertEqual(len(blind), 1)
        self.assertEqual(blind[0]["_group"], "B")

    def test_both_valid_sides_produce_paired_items(self):
        from batch import _process_one
        with patch("batch.localize", return_value={"copy": "B", "final_status": "pass"}), \
             patch("batch.localize_baseline", return_value={"copy": "A"}):
            _, blind, skipped = _process_one({"id": "C01", "text": "文案"}, "th", None, True)
        self.assertEqual({item["_group"] for item in blind}, {"A", "B"})
        self.assertEqual(skipped, [])


class TestBatchArtifacts(unittest.TestCase):
    def test_failed_pair_writes_skipped_artifact(self):
        import batch
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "creatives.json"
            source.write_text(json.dumps([{"id": "C01", "text": "文案"}], ensure_ascii=False), encoding="utf-8")
            with patch.object(batch, "BASE_DIR", str(root)), \
                 patch("batch.load_brand_context", return_value=None), \
                 patch("batch.localize", return_value={"copy": "", "final_status": "error"}), \
                 patch("batch.localize_baseline", return_value={"copy": "A"}), \
                 patch("batch.random.shuffle", side_effect=lambda items: None):
                paths = batch.run_batch(str(source), ["th"], with_baseline=True, workers=1)
            self.assertEqual(set(paths), {"batch", "blind", "key", "skipped"})
            self.assertEqual(json.loads(Path(paths["blind"]).read_text(encoding="utf-8")), [])


class TestBusinessLayers(unittest.TestCase):
    def test_french_profile_exposes_traceable_evidence_metadata(self):
        from profile_insights import load_profile_summary

        summary = load_profile_summary("fr", category="服饰", platform="Meta")
        self.assertTrue(summary["evidence_ids"])
        self.assertEqual(summary["evidence_ids"], [item["id"] for item in summary["evidence"]])
        self.assertTrue(all("evidence_level" in item for item in summary["evidence"]))
        self.assertTrue(all("validation_status" in item for item in summary["evidence"]))
        self.assertTrue(any(item.get("publisher") == "ARPP" for item in summary["risk_evidence"]))
        self.assertTrue(summary["source_urls"])
        self.assertTrue(summary["evidence_details"])
        self.assertIn(summary["evidence_details"][0]["evidence_level"], {"A", "B", "C"})
        self.assertIn("publisher", summary)
        self.assertIn("evidence_level", summary)
        self.assertTrue(0.0 < summary["confidence"] < 1.0)

    def test_french_insight_carries_evidence_details(self):
        from feishu_connector import build_market_insight

        insight = build_market_insight({"目标市场": "fr", "平台": "Meta", "产品品类": "服饰"})
        self.assertTrue(insight["evidence_ids"])
        self.assertEqual(insight["evidence_ids"], [item["id"] for item in insight["evidence"]])
        self.assertIn("evidence_level", insight["evidence"][0])
        self.assertIn("validation_status", insight["evidence"][0])
        self.assertIn("unverified_claims", insight)
        self.assertTrue(insight["source_urls"])
        self.assertTrue(insight["evidence_details"])
        self.assertTrue(insight["risk_evidence"])
        self.assertTrue(any(item.get("publisher") == "ARPP" for item in insight["risk_evidence"]))

    def test_french_fashion_demo_has_three_real_pipeline_variants_and_risk_case(self):
        from generate_demo_meta_fr_fashion import build_demo_package

        def runner(source, market, brand=None, verbose=False):
            risk = "瘦十斤" in source or "纸片人" in source
            return {
                "copy": "Une maille douce pour bouger librement." if not risk else "Devenez mince en un instant.",
                "copy_zh": "柔软针织，让活动更自在。",
                "final_status": "needs_review" if risk else "pass",
                "elements": {"selling_points": ["舒适"], "emotion_hook": "自在", "target_audience": "成人", "cta": "了解更多"},
                "fidelity": {"checks": [{"kind": "selling_point", "element": "舒适", "recovered": True}], "recovery_rate": 1.0},
                "taboo": {"risk_level": "high" if risk else "low", "flags": []},
                "profile_trace": {"valid_ids": ["fr-003"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
                "used_entries": ["fr-003"], "profile_version": "v0.1",
            }

        package = build_demo_package(runner=runner)
        self.assertEqual(package["market"], "fr")
        self.assertEqual(package["platform"], "Meta")
        self.assertEqual(package["category"], "服饰")
        self.assertEqual(len(package["variants"]), 3)
        for variant in package["variants"]:
            result = variant["localpipe_result"]
            self.assertIn("fidelity", result)
            self.assertIn("taboo", result)
            self.assertIn("profile_trace", result)
            self.assertIn("kreado_brief", variant)
        self.assertEqual(package["risk_case"]["localpipe_result"]["final_status"], "needs_review")
        self.assertEqual(package["risk_case"]["localpipe_result"]["taboo"]["risk_level"], "high")

    def test_process_tasks_uses_brand_from_each_task(self):
        from feishu_connector import process_tasks

        calls = []

        def runner(source, market, brand=None, verbose=False):
            calls.append(brand)
            return {"copy": "ok", "elements": {"selling_points": ["卖点"], "cta": "购买"},
                    "final_status": "pass", "fidelity": {"recovery_rate": 1},
                    "taboo": {"risk_level": "low"}, "used_entries": ["kr-003"],
                    "profile_version": "v0.1"}

        process_tasks([
            {"中文原文": "A", "目标市场": "kr", "品牌要求": "Brand A"},
            {"中文原文": "B", "目标市场": "kr", "品牌要求": "Brand B"},
            {"中文原文": "C", "目标市场": "kr"},
        ], runner=runner)

        self.assertEqual(["Brand A", "Brand B", None], [call and call.get("brand_name") for call in calls])

    def test_task_brand_extracts_name_from_chinese_requirements(self):
        from feishu_connector import _task_brand

        brand = _task_brand({"品牌要求": "雾川：温柔、简约、都市感；避免外貌羞辱和绝对化身材承诺"})
        self.assertEqual(brand["brand_name"], "雾川")
        self.assertEqual(brand["protected_terms"], [{"term": "雾川", "rule": "品牌名保持原样"}])
        self.assertIn("温柔", brand["tone"])

    def test_market_insight_is_derived_from_profile_entries(self):
        from feishu_connector import build_market_insight
        from profile_insights import load_profile_summary

        summary = load_profile_summary("kr", category="服饰", platform="Meta")
        result = build_market_insight({"目标市场": "kr", "平台": "Meta", "产品品类": "服饰"})
        self.assertEqual(result["evidence_ids"], summary["evidence_ids"])
        self.assertEqual(result["confidence"], summary["confidence"])
        self.assertTrue(any(item["id"] == "kr-003" for item in summary["evidence"]))
        self.assertNotIn("kr-007", result["evidence_ids"] if "kr-007" not in summary["evidence_ids"] else [])

    def test_strategy_uses_profile_summary_fields(self):
        from strategy import build_strategy

        result = build_strategy({
            "market": "kr", "platform": "Meta", "audience": "25-35岁女性",
            "selling_points": ["弹力面料"], "cta": "立即购买",
            "profile_summary": {
                "tone": "PROFILE_TONE", "scene": "PROFILE_SCENE", "risk_notes": "PROFILE_RISK",
                "evidence_ids": ["kr-test"], "confidence": 0.61,
            },
        })
        self.assertEqual(result["tone_direction"], "PROFILE_TONE")
        self.assertEqual(result["scene_direction"], "PROFILE_SCENE")
        self.assertEqual(result["risk_notes"], "PROFILE_RISK")
        self.assertEqual(result["evidence_ids"], ["kr-test"])
        self.assertEqual(result["confidence"], 0.61)

    def test_demo_variant_contract(self):
        from generate_demo_meta_kr_fashion import build_demo_package

        package = build_demo_package(
            runner=lambda *args, **kwargs: {
                "copy": "한국어 문안", "copy_zh": "中文回译", "final_status": "pass",
                "elements": {"selling_points": ["舒适"], "emotion_hook": "轻松", "target_audience": "女性", "cta": "购买"},
                "fidelity": {"checks": [{"kind": "selling_point", "element": "舒适", "recovered": True}], "recovery_rate": 1.0},
                "taboo": {"risk_level": "low", "flags": []},
                "profile_trace": {"valid_ids": ["kr-003"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
                "used_entries": ["kr-003"], "profile_version": "v0.1",
            }
        )
        self.assertEqual(len(package["variants"]), 3)
        for variant in package["variants"]:
            self.assertIn("fidelity", variant["localpipe_result"])
            self.assertIn("taboo", variant["localpipe_result"])
            self.assertIn("profile_trace", variant["localpipe_result"])
            self.assertIn("kreado_brief", variant)
        self.assertEqual(len({variant["kreado_brief"]["prompt"] for variant in package["variants"]}), 3)

    def test_market_insight_has_evidence_and_confidence(self):
        from feishu_connector import build_market_insight
        result = build_market_insight({"目标市场": "kr", "平台": "Meta", "目标人群": "年轻女性"})
        self.assertTrue(result["evidence_ids"])
        self.assertGreaterEqual(result["confidence"], 0)
        self.assertIn("Meta", result["market_summary"])

    def test_creative_package_exposes_feishu_evidence_fields(self):
        from feishu_connector import _merge_package_fields

        fields = {}
        _merge_package_fields(fields, {
            "insight": {
                "market_summary": "法国服饰洞察",
                "audience_pain_points": "材质和场景",
                "platform_preference": "Meta",
                "creative_direction": "真实使用",
                "risk_notes": "避免身材羞辱",
                "evidence_ids": ["fr-009"],
                "evidence_details": [{"id": "fr-009", "publisher": "FEVAD", "evidence_level": "A"}],
                "evidence_levels": ["A"],
                "source_urls": ["https://www.fevad.com/edition-2025-des-chiffres-cles-du-e-commerce/"],
                "validation_status": "待母语者校准",
                "unverified_claims": ["服饰适用性待验证"],
                "confidence": 0.7,
            },
            "strategy": {},
            "kreado": {"prompt": "p", "json": {}},
        })
        self.assertEqual(fields["证据等级"], "A")
        self.assertIn("FEVAD", fields["证据明细"])
        self.assertIn("fevad.com", fields["来源URL"])
        self.assertEqual(fields["画像校准状态"], "待母语者校准")

    def test_output_converts_numeric_confidence_for_text_compatible_feishu_field(self):
        from feishu_connector import _merge_package_fields

        fields = {}
        _merge_package_fields(fields, {
            "insight": {
                "market_summary": "x", "audience_pain_points": "x", "platform_preference": "x",
                "creative_direction": "x", "risk_notes": "x", "evidence_ids": [],
                "evidence_details": [], "evidence_levels": [], "source_urls": [],
                "validation_status": "x", "unverified_claims": [], "confidence": 0.61,
            },
            "strategy": {}, "kreado": {"prompt": "p", "json": {}},
        })
        self.assertIsInstance(fields["洞察置信度"], str)
        self.assertEqual(fields["洞察置信度"], "0.61")

    def test_market_insight_changes_with_product_category(self):
        from feishu_connector import build_market_insight
        from profile_insights import load_profile_summary
        result = build_market_insight({
            "目标市场": "kr", "平台": "Meta", "产品品类": "服饰", "目标人群": "25-35岁女性"
        })
        summary = load_profile_summary("kr", category="服饰", platform="Meta")
        self.assertEqual(result["evidence_ids"], summary["evidence_ids"])
        self.assertTrue(result["evidence_sources"])
        self.assertNotIn("手机发热", result["audience_pain_points"])

    def test_creative_package_accepts_plain_chinese_task_fields(self):
        from feishu_connector import build_creative_package

        task = {
            "目标市场": "kr",
            "平台": "Meta",
            "产品品类": "服饰",
            "目标人群": "25-35岁女性",
        }
        result = {
            "copy": "오늘도 편하게 입어요.",
            "elements": {
                "selling_points": ["弹力面料", "通勤约会两用"],
                "cta": "立即购买",
                "target_audience": "25-35岁女性",
            },
        }
        package = build_creative_package(task, result)
        self.assertEqual(package["strategy"]["market"], "kr")
        self.assertEqual(package["strategy"]["selling_points"], ["弹力面料", "通勤约会两用"])
        self.assertEqual(package["kreado"]["json"]["market"], "kr")

    def test_creative_package_accepts_nested_feishu_fields_and_aliases(self):
        from feishu_connector import build_creative_package

        task = {
            "record_id": "rec_demo",
            "fields": {
                "market": "kr",
                "platform": "Meta",
                "category": "服饰",
                "audience": "25-35岁女性",
            },
        }
        result = {
            "copy": "오늘도 편하게 입어요.",
            "elements": {"selling_points": ["弹力面料"], "cta": "立即购买"},
        }
        package = build_creative_package(task, result)
        self.assertEqual(package["insight"]["category"], "服饰")
        self.assertEqual(package["strategy"]["audience"], "25-35岁女性")

    def test_output_brief_is_executable_contract(self):
        from feishu_connector import build_output
        fields = build_output(
            {"任务ID": "T1", "目标市场": "kr"},
            {"copy": "한국어 광고", "copy_zh": "中文回译", "adaptation_note": "本地化",
             "used_entries": ["kr-001"], "profile_version": "v0.1", "final_status": "pass",
             "fidelity": {"recovery_rate": 1}, "taboo": {"risk_level": "low"},
             "elements": {"selling_points": ["轻薄"], "emotion_hook": "焦虑",
                          "target_audience": "年轻女性", "cta": "购买"}, "errors": None},
        )
        brief = json.loads(fields["下游素材Brief"])
        self.assertEqual(brief["target_audience"], "年轻女性")
        self.assertEqual(brief["cta"], "购买")

    def test_strategy_rejects_missing_market_or_platform(self):
        from strategy import build_strategy
        with self.assertRaises(ValueError):
            build_strategy({"source_text": "广告", "market": "kr"})

    def test_strategy_selects_platform_specific_hook(self):
        from strategy import build_strategy
        result = build_strategy({
            "source_text": "针织衫换季上新",
            "market": "kr",
            "platform": "Meta",
            "audience": "25-35岁女性",
            "selling_points": ["显瘦", "弹力不勒"],
            "cta": "立即购买",
        })
        self.assertEqual(result["platform"], "Meta")
        self.assertTrue(result["hook"])
        self.assertTrue(result["creative_angles"])

    def test_kreado_adapter_emits_prompt_and_json(self):
        from kreado_adapter import to_kreado_brief
        result = to_kreado_brief({
            "market": "kr", "platform": "Meta", "copy": "한국어 광고",
            "hook": "前三秒展示穿着前后对比", "audience": "年轻女性",
            "selling_points": ["显瘦"], "cta": "立即购买",
            "visual_direction": "通勤试穿", "duration_seconds": 15,
        })
        self.assertIn("prompt", result)
        self.assertEqual(result["json"]["duration_seconds"], 15)
        self.assertIn("Meta", result["prompt"])


class _FakeBitableClient:
    """内存版 Bitable 客户端，隔离真实 API（只实现闭环用到的方法）。"""

    def __init__(self, review_table="review", revision_table="revision", output_table="output"):
        self._tables = {}
        self._seq = 0
        self.review_table = review_table
        self.revision_table = revision_table
        self.output_table = output_table
        self.output_app_token = self.review_app_token = self.revision_app_token = "app"

    def list_records(self, table_id, app_token=None):
        return list(self._tables.get(table_id, []))

    def create_record(self, table_id, fields, app_token=None):
        self._seq += 1
        rid = f"rec{self._seq:03d}"
        self._tables.setdefault(table_id, []).append({"record_id": rid, "fields": dict(fields)})
        return rid

    def update_record(self, table_id, record_id, fields, app_token=None):
        for rec in self._tables.get(table_id, []):
            if rec["record_id"] == record_id:
                rec["fields"].update(fields)
                return
        raise KeyError(record_id)


class TestFeishuClosedLoop(unittest.TestCase):
    def _output_records(self):
        return [
            {"record_id": "out-001", "fields": {"任务ID": "T1", "目标市场": "kr", "系统状态": "pass", "画像条目": "kr-003,kr-006"}},
            {"record_id": "out-002", "fields": {"任务ID": "T2", "目标市场": "kr", "系统状态": "needs_review", "画像条目": "kr-001"}},
            {"record_id": "out-003", "fields": {"任务ID": "T3", "目标市场": "kr", "系统状态": "error", "画像条目": ""}},
            {"record_id": "out-004", "fields": {"任务ID": "T4", "目标市场": "jp", "系统状态": "pass", "画像条目": "jp-001"}},
        ]

    def test_sync_reviews_creates_and_is_idempotent(self):
        from feishu_connector import sync_reviews
        client = _FakeBitableClient()
        client._tables["output"] = self._output_records()
        self.assertEqual(sync_reviews(client), 3)  # out-003 error 跳过
        reviews = client._tables["review"]
        self.assertEqual(len(reviews), 3)
        for r in reviews:
            self.assertEqual(r["fields"]["归纳状态"], "待归纳")
            self.assertEqual(r["fields"]["审核记录ID"], r["record_id"])
        self.assertEqual(sync_reviews(client), 0)  # 幂等：不重复建
        client._tables["output"].append(
            {"record_id": "out-009", "fields": {"任务ID": "T9", "目标市场": "kr", "系统状态": "pass"}}
        )
        self.assertEqual(sync_reviews(client), 1)

    def test_sync_reviews_market_filter(self):
        from feishu_connector import sync_reviews
        client = _FakeBitableClient()
        client._tables["output"] = self._output_records()
        self.assertEqual(sync_reviews(client, "jp"), 1)
        reviews = client._tables["review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["fields"]["目标市场"], "jp")

    def _ai_result(self):
        return {
            "problem_categories": [{"category": "语气", "count": 2, "summary": "敬语基调不稳"}],
            "feedback_summary": "整体自然度中等，敬语与平语混用需统一。",
            "revision_candidates": [
                {"action": "new", "target_entry_id": None, "entry_type": "语言风格",
                 "content": "新条目", "confidence": "中", "expires": None, "reason": "多轮反馈"},
                {"action": "modify", "target_entry_id": "kr-003", "entry_type": "语言风格",
                 "content": "修改后", "confidence": "高", "expires": "2026-12-31", "reason": "反馈X"},
            ],
        }

    def test_summarize_reviews_writes_ai_fields_and_candidates(self):
        from feishu_connector import summarize_reviews
        client = _FakeBitableClient()
        client._tables["review"] = [
            {"record_id": "rv-001", "fields": {"审核记录ID": "rv-001", "目标市场": "kr", "归纳状态": "待归纳",
                "自然度": 3, "地道感": 3, "广告吸引力": 4, "采用意见": "修改后通过", "问题类型": "语气",
                "原始反馈": "敬语混乱", "修改建议": "统一敬语"}},
            {"record_id": "rv-002", "fields": {"审核记录ID": "rv-002", "目标市场": "kr", "归纳状态": "待归纳",
                "自然度": 4, "地道感": 4, "广告吸引力": 4, "采用意见": "通过", "问题类型": "",
                "原始反馈": "", "修改建议": ""}},
        ]
        calls = []

        def runner(reviews, market_code):
            calls.append((reviews, market_code))
            return self._ai_result()

        n = summarize_reviews(client, runner=runner)
        self.assertEqual(n, 2)
        self.assertEqual(calls[0][1], "kr")
        self.assertEqual(len(calls[0][0]), 2)
        for r in client._tables["review"]:
            self.assertEqual(r["fields"]["归纳状态"], "已归纳")
            self.assertIn("AI问题归类", r["fields"])
            self.assertIn("AI反馈总结", r["fields"])
        cands = client._tables["revision"]
        self.assertEqual(len(cands), 2)
        for c in cands:
            self.assertEqual(c["fields"]["状态"], "待确认")
            self.assertEqual(c["fields"]["目标市场"], "kr")
            self.assertIn("rv-001", c["fields"]["引用审核记录"])
            self.assertIn("rv-002", c["fields"]["引用审核记录"])
        self.assertEqual({c["fields"]["动作"] for c in cands}, {"新增", "修改"})

    def test_summarize_reviews_empty_candidates(self):
        from feishu_connector import summarize_reviews
        client = _FakeBitableClient()
        client._tables["review"] = [
            {"record_id": "rv-001", "fields": {"审核记录ID": "rv-001", "目标市场": "kr", "归纳状态": "待归纳",
                "原始反馈": "ok", "采用意见": "通过"}},
        ]
        n = summarize_reviews(client, runner=lambda reviews, m: {
            "problem_categories": [], "feedback_summary": "无大问题", "revision_candidates": [],
        })
        self.assertEqual(n, 0)
        self.assertEqual(client._tables.get("revision", []), [])
        self.assertEqual(client._tables["review"][0]["fields"]["归纳状态"], "已归纳")

    def test_apply_revisions_reads_adopted_and_marks_applied(self):
        from feishu_connector import apply_revisions
        client = _FakeBitableClient()
        client._tables["revision"] = [
            {"record_id": "c-001", "fields": {"目标市场": "kr", "状态": "已采纳", "动作": "新增",
                "目标条目ID": "", "条目类型": "语言风格", "新条目内容": "新条目", "建议置信度": "中",
                "建议过期时间": "", "依据理由": "多轮反馈"}},
            {"record_id": "c-002", "fields": {"目标市场": "kr", "状态": "已采纳", "动作": "过期",
                "目标条目ID": "kr-003", "条目类型": "", "新条目内容": "", "建议置信度": "",
                "建议过期时间": "", "依据理由": ""}},
            {"record_id": "c-003", "fields": {"目标市场": "kr", "状态": "待确认", "动作": "修改",
                "目标条目ID": "kr-006", "条目类型": "", "新条目内容": "改", "建议置信度": "高",
                "建议过期时间": "", "依据理由": ""}},
            {"record_id": "c-004", "fields": {"目标市场": "jp", "状态": "已采纳", "动作": "修改",
                "目标条目ID": "jp-001", "条目类型": "", "新条目内容": "改", "建议置信度": "中",
                "建议过期时间": "", "依据理由": ""}},
        ]
        captured = {}

        def fake_apply(m, cands):
            captured[m] = cands
            return "v0.3"

        with patch("feishu_connector.apply_revisions_to_profile", side_effect=fake_apply), \
             patch("feishu_connector.gen_profile_hashes") as gh:
            n = apply_revisions(client)
        self.assertEqual(n, 3)  # c-003 待确认跳过
        self.assertEqual(set(captured.keys()), {"kr", "jp"})
        self.assertEqual([c["action"] for c in captured["kr"]], ["new", "expire"])
        self.assertEqual(captured["kr"][0]["content"], "新条目")
        self.assertEqual(captured["jp"][0]["action"], "modify")
        status = {r["record_id"]: r["fields"]["状态"] for r in client._tables["revision"]}
        self.assertEqual(status["c-001"], "已应用")
        self.assertEqual(status["c-002"], "已应用")
        self.assertEqual(status["c-003"], "待确认")
        self.assertEqual(status["c-004"], "已应用")
        gh.assert_called_once()

    def test_apply_revisions_market_filter(self):
        from feishu_connector import apply_revisions
        client = _FakeBitableClient()
        client._tables["revision"] = [
            {"record_id": "c-001", "fields": {"目标市场": "kr", "状态": "已采纳", "动作": "新增",
                "条目类型": "语言风格", "新条目内容": "新", "建议置信度": "中"}},
            {"record_id": "c-002", "fields": {"目标市场": "jp", "状态": "已采纳", "动作": "修改",
                "目标条目ID": "jp-001", "新条目内容": "改", "建议置信度": "中"}},
        ]
        with patch("feishu_connector.apply_revisions_to_profile", return_value="v0.2"), \
             patch("feishu_connector.gen_profile_hashes"):
            n = apply_revisions(client, "kr")
        self.assertEqual(n, 1)
        status = {r["record_id"]: r["fields"]["状态"] for r in client._tables["revision"]}
        self.assertEqual(status["c-001"], "已应用")
        self.assertEqual(status["c-002"], "已采纳")

    def test_apply_revisions_to_profile_atomic_write(self):
        from review_ai import apply_revisions_to_profile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zz.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "market": "测试", "market_code": "zz", "version": "v0.1", "language": "zz",
                    "entries": [
                        {"id": "zz-001", "type": "语言风格", "content": "旧", "confidence": "中", "expires": None, "source": "s"},
                        {"id": "zz-002", "type": "消费习惯", "content": "留", "confidence": "低", "expires": None, "source": "s"},
                        {"id": "zz-003", "type": "文化禁忌", "content": "删", "confidence": "高", "expires": None, "source": "s"},
                    ],
                }, f, ensure_ascii=False, indent=2)
            version = apply_revisions_to_profile("zz", [
                {"action": "new", "entry_type": "市场趋势", "content": "新条目", "confidence": "高",
                 "expires": None, "reason": "多轮反馈"},
                {"action": "modify", "target_entry_id": "zz-001", "content": "改后", "confidence": "高",
                 "expires": "2026-12-31", "reason": "反馈"},
                {"action": "expire", "target_entry_id": "zz-002", "reason": ""},
                {"action": "delete", "target_entry_id": "zz-003", "reason": ""},
            ], profile_path=path)
            self.assertEqual(version, "v0.2")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["version"], "v0.2")
            self.assertEqual(data["updated"], date.today().isoformat())
            by_id = {e["id"]: e for e in data["entries"]}
            self.assertIn("zz-004", by_id)  # 新条目分配下一个 id
            self.assertEqual(by_id["zz-004"]["content"], "新条目")
            self.assertEqual(by_id["zz-004"]["type"], "市场趋势")
            self.assertIn("人工审核反馈归纳", by_id["zz-004"]["source"])
            self.assertEqual(by_id["zz-001"]["content"], "改后")
            self.assertEqual(by_id["zz-001"]["expires"], "2026-12-31")
            self.assertEqual(by_id["zz-002"]["expires"], date.today().isoformat())
            self.assertNotIn("zz-003", by_id)
            self.assertFalse(os.path.exists(path + ".tmp"))  # 无临时文件残留

    def test_apply_revisions_rejects_entry_without_id(self):
        from review_ai import apply_revisions_to_profile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zz.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "market": "测试", "market_code": "zz", "version": "v0.1", "language": "zz",
                    "entries": [
                        {"type": "语言风格", "content": "无ID条目", "confidence": "中", "expires": None, "source": "s"},
                        {"id": "zz-001", "type": "语言风格", "content": "旧", "confidence": "中", "expires": None, "source": "s"},
                    ],
                }, f, ensure_ascii=False, indent=2)
            with self.assertRaises(ValueError):
                apply_revisions_to_profile("zz", [
                    {"action": "modify", "target_entry_id": "zz-001", "content": "改"},
                ], profile_path=path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["version"], "v0.1")  # 未落盘
            self.assertEqual(data["entries"][0]["content"], "无ID条目")
            self.assertFalse(os.path.exists(path + ".tmp"))


class TestBorrowedIndustryMethods(unittest.TestCase):
    def test_hofstede_background_is_not_exposed_as_a_citable_profile_entry(self):
        from pipeline import profile_context

        context = profile_context({
            "entries": [
                {"id": "fr-001", "type": "语言风格", "confidence": "中", "content": "使用自然法语"},
            ],
            "cultural_dimensions": {
                "dimensions": {"individualism": 71},
                "usage": "不得单独推出广告结论",
            },
        })

        self.assertIn("[fr-001]", context)
        self.assertNotIn("[hofstede]", context)
        self.assertIn("不得写入 used_entries", context)

    def test_mqm_quality_report_maps_failures_and_release_decision(self):
        from quality_framework import build_quality_report

        report = build_quality_report({
            "final_status": "needs_review",
            "fidelity": {
                "recovery_rate": 0.5,
                "recovery_rate_unweighted": 0.75,
                "_failed": [
                    {"kind": "protected_term", "element": "雾川", "reason": "not_recovered"},
                    {"kind": "emotion_hook", "element": "轻松换季", "reason": "not_recovered"},
                ],
            },
            "taboo": {"risk_level": "medium", "flags": [{"entry_id": "fr-002", "detail": "体重承诺"}]},
            "profile_trace": {"valid_ids": ["fr-001"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
        })

        self.assertEqual(report["framework"], "MQM-inspired advertising transcreation QA")
        self.assertEqual(report["release_decision"], "block")
        self.assertTrue(any(x["category"] == "terminology" and x["severity"] == "critical" for x in report["issues"]))
        self.assertTrue(any(x["category"] == "cultural_compliance" and x["severity"] == "major" for x in report["issues"]))
        self.assertEqual(report["weighted_fidelity"], 0.5)
        self.assertEqual(report["unweighted_fidelity"], 0.75)

    def test_language_assets_preserve_brand_rules_and_audit_copy(self):
        from language_assets import audit_language_assets, build_language_assets

        assets = build_language_assets(
            {
                "brand_name": "雾川",
                "brand_name_rule": "保持中文原名",
                "protected_terms": [{"term": "雾川", "rule": "必须保留"}],
                "tone": "温柔、简洁",
                "avoid": ["瘦十斤", "纸片人"],
            },
            {"evidence_ids": ["fr-001"], "validation_status": "待母语者校准"},
        )
        self.assertEqual(assets["brand"]["name"], "雾川")
        self.assertEqual(assets["protected_terms"][0]["term"], "雾川")
        self.assertEqual(assets["evidence_ids"], ["fr-001"])

        audit = audit_language_assets("这件针织衫穿上瘦十斤", assets)
        self.assertIn("雾川", audit["missing_protected_terms"])
        self.assertIn("瘦十斤", audit["forbidden_terms_found"])
        self.assertFalse(audit["pass"])

    def test_pending_brand_form_requires_review_instead_of_being_approved(self):
        from language_assets import audit_language_assets, build_language_assets

        assets = build_language_assets({
            "brand_name": "雾川",
            "brand_name_rule": "法国市场名称待母语者确认",
            "approved_forms": ["雾川"],
            "candidate_forms": [
                {"term": "Wuchuan", "status": "pending_native_validation"},
            ],
        })

        audit = audit_language_assets("Découvrez Wuchuan.", assets)
        self.assertEqual(audit["approved_forms_found"], [])
        self.assertEqual(audit["pending_candidate_forms_found"], ["Wuchuan"])
        self.assertEqual(audit["release_decision"], "needs_review")
        self.assertFalse(audit["pass"])

    def test_creative_matrix_builds_three_differentiated_evidence_linked_routes(self):
        from creative_matrix import build_creative_matrix

        routes = build_creative_matrix({
            "platform": "Meta",
            "selling_points": ["柔软针织", "方便叠穿", "活动自在"],
            "scene_direction": "办公室、咖啡馆和周末散步",
            "tone_direction": "自然、克制",
            "hook": "前三秒展示真实使用细节",
            "cta": "了解系列",
            "evidence_ids": ["fr-001", "fr-003"],
        })
        self.assertEqual(len(routes), 3)
        self.assertEqual(len({x["route_id"] for x in routes}), 3)
        self.assertEqual(len({x["objective"] for x in routes}), 3)
        self.assertTrue(all(x["evidence_ids"] == ["fr-001", "fr-003"] for x in routes))
        self.assertTrue(all(x["visual_direction"] for x in routes))

    def test_blind_metrics_reveal_groups_and_separate_safety_case(self):
        from experiment_metrics import summarize_blind_results

        key = [
            {"sample_id": "FR01-B", "creative_id": "FR01", "role": "主效果样本", "x_group": "Baseline", "y_group": "LocalPipe"},
            {"sample_id": "FR03-B", "creative_id": "FR03", "role": "独立安全校验样本", "x_group": "Baseline", "y_group": "LocalPipe"},
        ]
        responses = [
            {"sample_id": "FR01-B", "q1": 4, "q2": 5, "q3": 3, "q4_x": "B", "q4_y": "A", "q5": "N"},
            {"sample_id": "FR03-B", "q1": 5, "q2": 4, "q3": 4, "q4_x": "D", "q4_y": "A", "q5": "X"},
        ]
        metrics = summarize_blind_results(responses, key)
        self.assertEqual(metrics["main_quality"]["q2"]["localpipe_wins"], 1)
        self.assertEqual(metrics["main_quality"]["q3"]["ties"], 1)
        self.assertEqual(metrics["main_quality"]["q2"]["non_tie_win_rate"], 1.0)
        self.assertEqual(metrics["main_quality"]["publication_usability"]["LocalPipe"], 1.0)
        self.assertEqual(metrics["safety_cases"][0]["creative_id"], "FR03")

    def test_transcreation_delivery_contains_industry_standard_artifacts(self):
        from transcreation_delivery import build_transcreation_delivery

        delivery = build_transcreation_delivery(
            result={
                "copy": "Une maille douce pour le quotidien.",
                "copy_zh": "适合日常穿着的柔软针织。",
                "adaptation_note": "将夸张卖点改为真实场景。",
                "final_status": "pass",
                "fidelity": {"recovery_rate": 1.0, "_failed": []},
                "taboo": {"risk_level": "low", "flags": []},
                "profile_trace": {"valid_ids": ["fr-001"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
            },
            route={"route_id": "scene_proof", "objective": "场景证明", "recommended_use": "Meta 主文案"},
            kreado={"prompt": "prompt", "json": {"market": "fr"}},
            language_assets={"brand": {"name": "雾川"}},
        )
        self.assertEqual(delivery["target_copy"], "Une maille douce pour le quotidien.")
        self.assertEqual(delivery["back_translation_zh"], "适合日常穿着的柔软针织。")
        self.assertEqual(delivery["creative_rationale"], "将夸张卖点改为真实场景。")
        self.assertEqual(delivery["recommended_use"], "Meta 主文案")
        self.assertIn("quality_report", delivery)
        self.assertEqual(delivery["kreado_brief"]["prompt"], "prompt")

    def test_delivery_combines_pipeline_and_language_asset_decisions(self):
        from transcreation_delivery import build_transcreation_delivery

        delivery = build_transcreation_delivery(
            result={
                "copy": "Découvrez Wuchuan.",
                "copy_zh": "了解雾川。",
                "adaptation_note": "品牌法语写法待确认。",
                "final_status": "pass",
                "fidelity": {"recovery_rate": 1.0, "_failed": []},
                "taboo": {"risk_level": "low", "flags": []},
                "profile_trace": {"valid_ids": ["fr-001"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
            },
            route={"route_id": "brand", "recommended_use": "Meta 候选"},
            kreado={"prompt": "prompt", "json": {}},
            language_assets={
                "brand": {"name": "雾川", "approved_forms": ["雾川"]},
                "protected_terms": [],
                "candidate_forms": [{"term": "Wuchuan", "status": "pending_native_validation"}],
                "forbidden_terms": [],
            },
        )

        self.assertEqual(delivery["quality_report"]["release_decision"], "publish")
        self.assertEqual(delivery["language_asset_audit"]["release_decision"], "needs_review")
        self.assertEqual(delivery["final_delivery_decision"], "needs_review")

    def test_in_country_review_category_normalization(self):
        from review_ai import normalize_review_category

        self.assertEqual(normalize_review_category("品牌名问题"), "品牌/术语")
        self.assertEqual(normalize_review_category("虚构信息"), "新增事实")
        self.assertEqual(normalize_review_category("身材羞辱"), "文化/合规")
        self.assertEqual(normalize_review_category("未知标签"), "其他")

    def test_creative_package_exposes_quality_assets_and_delivery(self):
        from feishu_connector import build_creative_package

        package = build_creative_package(
            {
                "目标市场": "fr", "平台": "Meta", "产品品类": "服饰",
                "目标人群": "法国都市成年人", "品牌要求": "雾川：温柔、简洁；避免瘦十斤,纸片人",
            },
            {
                "copy": "Une maille douce pour le quotidien.",
                "copy_zh": "适合日常穿着的柔软针织。",
                "adaptation_note": "使用真实日常场景。",
                "final_status": "pass",
                "elements": {"selling_points": ["柔软针织"], "target_audience": "法国都市成年人", "cta": "了解系列"},
                "fidelity": {"recovery_rate": 1.0, "_failed": []},
                "taboo": {"risk_level": "low", "flags": []},
                "profile_trace": {"valid_ids": ["fr-001"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
            },
        )
        self.assertIn("language_assets", package)
        self.assertIn("quality_report", package)
        self.assertIn("transcreation_delivery", package)
        self.assertEqual(package["quality_report"]["release_decision"], "publish")
        self.assertEqual(package["transcreation_delivery"]["kreado_brief"], package["kreado"])

    def test_france_demo_exposes_matrix_route_and_transcreation_delivery(self):
        from generate_demo_meta_fr_fashion import build_demo_package

        def runner(*args, **kwargs):
            return {
                "copy": "Une maille douce pour le quotidien.", "copy_zh": "柔软针织，适合日常。",
                "adaptation_note": "按真实使用场景重构。", "final_status": "pass",
                "elements": {"selling_points": ["柔软针织", "方便叠穿", "活动自在"], "emotion_hook": "轻松", "target_audience": "成年人", "cta": "了解系列"},
                "fidelity": {"checks": [], "recovery_rate": 1.0, "_failed": []},
                "taboo": {"risk_level": "low", "flags": []},
                "profile_trace": {"valid_ids": ["fr-001"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
                "used_entries": ["fr-001"], "profile_version": "v0.2",
            }

        package = build_demo_package(runner=runner)
        self.assertEqual(package["variant_count"], 3)
        self.assertEqual(len({v["creative_route"]["route_id"] for v in package["variants"]}), 3)
        self.assertTrue(all("transcreation_delivery" in v for v in package["variants"]))
        self.assertTrue(all("quality_report" in v["transcreation_delivery"] for v in package["variants"]))

    def test_feishu_uses_existing_brief_field_for_transcreation_delivery(self):
        from feishu_connector import _merge_package_fields

        fields = {}
        package = {
            "insight": {
                "market_summary": "法国洞察", "audience_pain_points": "材质", "platform_preference": "Meta",
                "creative_direction": "真实场景", "risk_notes": "避免夸张", "evidence_ids": ["fr-001"],
                "evidence_details": [], "evidence_levels": ["C"], "source_urls": [],
                "validation_status": "待校准", "unverified_claims": [], "confidence": 0.7,
            },
            "strategy": {"market": "fr"},
            "kreado": {"prompt": "p", "json": {"market": "fr"}},
            "transcreation_delivery": {"target_copy": "Texte", "quality_report": {"release_decision": "publish"}},
        }
        _merge_package_fields(fields, package)
        delivered = json.loads(fields["下游素材Brief"])
        self.assertEqual(delivered["target_copy"], "Texte")
        self.assertEqual(delivered["quality_report"]["release_decision"], "publish")

    def test_review_record_is_normalized_before_ai_summary(self):
        from feishu_connector import _review_to_dict

        normalized = _review_to_dict({"fields": {"问题类型": "品牌名问题", "原始反馈": "Wu Chuan 不自然"}})
        self.assertEqual(normalized["问题类型"], "品牌/术语")

    def test_france_demo_runs_three_distinct_routed_briefs_through_localize(self):
        from generate_demo_meta_fr_fashion import SOURCE_BRIEF, build_demo_package

        seen = []

        def runner(source, *args, **kwargs):
            seen.append(source)
            return {
                "copy": f"Version {len(seen)}", "copy_zh": f"版本{len(seen)}", "adaptation_note": "按路线创作。",
                "final_status": "pass",
                "elements": {"selling_points": ["柔软针织", "方便叠穿", "活动自在"], "emotion_hook": "轻松", "target_audience": "成年人", "cta": "了解系列"},
                "fidelity": {"checks": [], "recovery_rate": 1.0, "_failed": []},
                "taboo": {"risk_level": "low", "flags": []},
                "profile_trace": {"valid_ids": ["fr-001"], "invalid_ids": [], "taboo_ids": [], "empty_reference": False},
                "used_entries": ["fr-001"], "profile_version": "v0.2",
            }

        package = build_demo_package(runner=runner)
        main_sources = seen[:3]
        self.assertEqual(len(set(main_sources)), 3)
        self.assertTrue(all(SOURCE_BRIEF in source for source in main_sources))
        self.assertEqual([v["localpipe_result"]["copy"] for v in package["variants"]], ["Version 1", "Version 2", "Version 3"])


if __name__ == "__main__":
    unittest.main()
