"""法国服饰样板：雾川针织衫 → LocalPipe → 法语广告（与韩版同源 brief，市场=fr）。

对齐 8/8 主线（Meta + 服饰品类）。产出供法语母语者问卷题目一评审。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict

from feishu_connector import build_creative_package
from kreado_adapter import to_kreado_brief
from pipeline import localize


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "outputs" / "demo_fr_fashion_package.json"

SOURCE_BRIEF = (
    "换季穿搭不想反复纠结？雾川舒适针织衫，柔软针织和弹力面料让活动更自在；"
    "一件衣服从通勤到周末约会都能自然搭配。请只围绕舒适、材质和使用场景表达，"
    "不讨论身材、体重、胖瘦或外貌评价。"
)

TASK = {
    "任务ID": "DEMO-FR-META-FASHION",
    "目标市场": "fr",
    "平台": "Meta",
    "产品品类": "服饰",
    "目标人群": "25-40岁法国都市成年人（注重品质与真实感）",
    "品牌要求": "雾川：温柔、简约、都市感；避免外貌羞辱和绝对化身材承诺",
}

BRAND = {
    "brand_name": "雾川",
    "brand_name_rule": "品牌名保持中文原样",
    "protected_terms": [{"term": "雾川", "rule": "品牌名保持原样"}],
    "tone": "温柔、简约、都市感",
    "do": ["强调舒适、材质和真实穿着场景"],
    "avoid": ["外貌羞辱", "体重承诺", "未经验证的绝对化效果"],
}


def build_demo_package(runner: Callable[..., Dict[str, Any]] = localize) -> Dict[str, Any]:
    result = runner(SOURCE_BRIEF, "fr", brand=BRAND, verbose=False)
    package = build_creative_package(TASK, result)
    kreado = to_kreado_brief(package["strategy"])
    return {
        "demo_status": "真实 localize() 产出；洞察和策略引用画像条目；非企业真实调研结论",
        "input_brief": SOURCE_BRIEF,
        "input_task": TASK,
        "market": "fr",
        "platform": "Meta",
        "category": "服饰",
        "localpipe_result": result,
        "market_insight": package["insight"],
        "creative_strategy": package["strategy"],
        "kreado_brief": kreado,
    }


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    package = build_demo_package()
    OUTPUT_PATH.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"演示样例已保存: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
