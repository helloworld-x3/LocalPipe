"""Generate the truthful Meta + Korea fashion demonstration package.

The demo calls the unchanged LocalPipe ``localize`` interface for every
variant. The variant angle is metadata for the demo brief; quality details are
always the returned pipeline details, never hand-written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

from feishu_connector import build_creative_package
from kreado_adapter import to_kreado_brief
from pipeline import localize


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "outputs" / "demo_meta_kr_fashion_package.json"

SOURCE_BRIEF = (
    "换季穿搭不想反复纠结？雾川舒适针织衫，柔软针织和弹力面料让活动更自在；"
    "一件衣服从通勤到周末约会都能自然搭配。请只围绕舒适、材质和使用场景表达，"
    "不讨论身材、体重、胖瘦或外貌评价。"
)

VARIANTS = [
    {
        "id": "comfort_real_use",
        "label": "舒适真实使用",
        "creative_angle": "用通勤、日常活动中的真实穿着细节证明舒适度",
    },
    {
        "id": "commute_to_date",
        "label": "通勤约会两用",
        "creative_angle": "同一件针织衫从工作日通勤切换到约会场景，突出搭配效率",
    },
    {
        "id": "material_detail_proof",
        "label": "面料细节证明",
        "creative_angle": "用针织纹理、回弹和活动镜头呈现可验证的材质利益点",
    },
]

FASHION_BRAND = {
    "brand_name": "雾川",
    "brand_name_rule": "品牌名保持中文原样",
    "protected_terms": [{"term": "雾川", "rule": "品牌名保持原样"}],
    "tone": "温柔、简约、都市感",
    "do": ["强调舒适、材质和真实穿着场景"],
    "avoid": ["外貌羞辱", "体重承诺", "未经验证的绝对化效果"],
}


def _task_for_variant(variant: Dict[str, str]) -> Dict[str, Any]:
    return {
        "任务ID": f"DEMO-KR-META-FASHION-{variant['id']}",
        "目标市场": "kr",
        "平台": "Meta",
        "产品品类": "服饰",
        "目标人群": "25-35岁韩国都市女性",
        "品牌要求": "雾川：温柔、简约、都市感；避免外貌羞辱和绝对化身材承诺",
    }


def build_demo_package(runner: Callable[..., Dict[str, Any]] = localize) -> Dict[str, Any]:
    brand = FASHION_BRAND
    variants: List[Dict[str, Any]] = []
    for variant in VARIANTS:
        task = _task_for_variant(variant)
        result = runner(SOURCE_BRIEF, "kr", brand=brand, verbose=False)
        package = build_creative_package(task, result)
        strategy = dict(package["strategy"])
        strategy["creative_angles"] = [variant["creative_angle"]] + list(strategy.get("creative_angles", []))
        strategy["visual_direction"] = variant["creative_angle"]
        kreado = to_kreado_brief(strategy)
        variants.append({
            "variant_id": variant["id"],
            "variant_label": variant["label"],
            "creative_angle": variant["creative_angle"],
            "input": task,
            "localpipe_result": result,
            "market_insight": package["insight"],
            "creative_strategy": strategy,
            "kreado_brief": kreado,
        })

    # C04 is retained as an explicit quality-risk case, not a successful demo.
    risk_source = (
        "姐妹们，真的不是PS！雾川落肩针织衫，上身直接瘦十斤，谁穿谁像纸片人。"
        "弹力面料不勒肉，通勤约会都能打，这波换季必入。"
    )
    risk_task = {**_task_for_variant({"id": "c04-risk", "label": "C04风险案例", "creative_angle": ""}), "任务ID": "C04-KR-RISK-CASE"}
    risk_result = runner(risk_source, "kr", brand=brand, verbose=False)

    return {
        "demo_status": "真实 localize() 产出；洞察和策略引用画像条目；非企业真实调研结论",
        "input_brief": SOURCE_BRIEF,
        "market": "kr",
        "platform": "Meta",
        "category": "服饰",
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
