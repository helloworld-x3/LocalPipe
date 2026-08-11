"""Generate the main Meta + France fashion demonstration package.

The default runner is the unchanged LocalPipe ``localize`` function.  The
three creative variants change the downstream strategy/brief angle while the
localized copy and all quality gates come from a real pipeline run.  A
separate C04-style body-shaming input is retained as a negative quality case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

from feishu_connector import build_creative_package
from creative_matrix import build_creative_matrix
from kreado_adapter import to_kreado_brief
from language_assets import build_language_assets
from pipeline import localize
from run_ledger import build_run_snapshot
from transcreation_delivery import build_transcreation_delivery


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "outputs" / "demo_meta_fr_fashion_package.json"

SOURCE_BRIEF = (
    "换季穿搭不想反复纠结？雾川舒适针织衫，柔软针织与弹力面料让活动更自在。"
    "一件衣服从通勤到周末约会都能自然搭配。请围绕舒适、材质和真实使用场景创作，"
    "不讨论身材、体重或外貌评价。"
)

VARIANTS = [
    {
        "id": "comfort_real_use",
        "label": "真实舒适",
        "creative_angle": "用通勤、步行和坐下起身的真实动作呈现柔软针织与活动自在感",
    },
    {
        "id": "commute_to_weekend",
        "label": "通勤到周末",
        "creative_angle": "同一件针织衫从工作日通勤自然切换到周末约会，突出搭配效率",
    },
    {
        "id": "material_detail",
        "label": "材质细节",
        "creative_angle": "用针织纹理、弹力回弹和近景触感镜头证明材质卖点，不做夸张承诺",
    },
]

FASHION_BRAND = {
    "brand_name": "雾川",
    "brand_name_rule": "中文原名已批准；法国市场拉丁字母写法待母语者与品牌方确认",
    "approved_forms": ["雾川"],
    "candidate_forms": [
        {
            "term": "Wuchuan",
            "status": "pending_native_validation",
            "evidence": "内部音译候选，尚未获得品牌方批准",
        },
        {
            "term": "Wu Chuan",
            "status": "pending_native_validation",
            "evidence": "一份法国母语者反馈认为不自然；等待另一份反馈后再定规则",
        },
    ],
    "protected_terms": [{"term": "雾川", "rule": "品牌名保持原样"}],
    "tone": "温柔、简约、都市感",
    "do": ["强调舒适、材质和真实穿着场景"],
    "avoid": ["外貌羞辱", "体重承诺", "未经验证的绝对化效果"],
}


def _task_for_variant(variant: Dict[str, str]) -> Dict[str, Any]:
    return {
        "任务ID": f"DEMO-FR-META-FASHION-{variant['id']}",
        "目标市场": "fr",
        "平台": "Meta",
        "产品品类": "服饰",
        "目标人群": "法国都市成年消费者",
        "品牌要求": "雾川：温柔、简约、都市感；避免外貌羞辱和绝对化身材承诺",
    }


def build_demo_package(runner: Callable[..., Dict[str, Any]] = localize) -> Dict[str, Any]:
    variants: List[Dict[str, Any]] = []
    routes = None
    for index, variant in enumerate(VARIANTS):
        task = _task_for_variant(variant)
        routed_source = (
            f"{SOURCE_BRIEF}\n"
            f"【本变体创意路线】{variant['creative_angle']}。"
            "产品事实、品牌要求和禁止事项保持不变，只调整创意切入与表达重点。"
        )
        result = runner(routed_source, "fr", brand=FASHION_BRAND, verbose=False)
        package = build_creative_package(task, result)
        strategy = dict(package["strategy"])
        if routes is None:
            routes = build_creative_matrix(strategy)
        route = routes[index]
        strategy["creative_angles"] = [route["objective"]] + list(strategy.get("creative_angles", []))
        strategy["visual_direction"] = route["visual_direction"]
        strategy["hook"] = route["hook"]
        kreado = to_kreado_brief(strategy)
        assets = build_language_assets(FASHION_BRAND, {
            "evidence_ids": strategy.get("evidence_ids", []),
            "validation_status": strategy.get("validation_status", "待人工复核"),
        })
        delivery = build_transcreation_delivery(result, route, kreado, assets)
        run_snapshot = build_run_snapshot(
            task,
            result,
            quality_decision=delivery["quality_report"]["release_decision"],
            strategy=strategy,
        )
        delivery["run_snapshot"] = run_snapshot
        variants.append({
            "variant_id": route["route_id"],
            "variant_label": route["objective"],
            "creative_angle": route["visual_direction"],
            "creative_route": route,
            "routed_brief": routed_source,
            "input": task,
            "localpipe_result": result,
            "market_insight": package["insight"],
            "creative_strategy": strategy,
            "kreado_brief": kreado,
            "run_snapshot": run_snapshot,
            "transcreation_delivery": delivery,
        })

    risk_source = (
        "姐妹们，真的不是PS！雾川落肩针织衫，上身直接瘦十斤，谁穿谁像纸片人。"
        "弹力面料不勒肉，通勤约会都能打，这波换季必入。"
    )
    risk_task = {**_task_for_variant({"id": "c04-risk", "label": "C04风险案例", "creative_angle": ""}), "任务ID": "C04-FR-RISK-CASE"}
    risk_result = runner(risk_source, "fr", brand=FASHION_BRAND, verbose=False)

    return {
        "demo_status": "真实 localize() 产出；法国洞察引用画像条目；公开证据、冷启动假设和待校准状态分层；非企业真实调研结论",
        "input_brief": SOURCE_BRIEF,
        "market": "fr",
        "platform": "Meta",
        "category": "服饰",
        "profile_version": variants[0]["localpipe_result"].get("profile_version", "") if variants else "",
        "variant_count": len(variants),
        "variants": variants,
        "risk_case": {"input": risk_task, "localpipe_result": risk_result},
    }


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    package = build_demo_package()
    OUTPUT_PATH.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"演示样例已保存: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
