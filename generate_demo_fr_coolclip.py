"""法国市场闭环演示：C01 CoolClip 散热背夹 → LocalPipe → 法语广告 + KreadoAI Brief。

源文案含「3秒降温15度」量化功效宣称，预期触发 fr-002 合规检查（欧盟 655/2013
需可验证证据），用于展示质检层拦截真实风险。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict

from feishu_connector import build_creative_package
from kreado_adapter import to_kreado_brief
from pipeline import localize


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "outputs" / "demo_fr_coolclip_package.json"

SOURCE_BRIEF = (
    "这个夏天，别让手机先中暑！CoolClip散热背夹，3秒降温15度，开黑五连坐照样稳如老狗。"
    "学生党福音，一杯奶茶钱，游戏体验直接起飞。"
)

SOURCE_TASK = {
    "任务ID": "DEMO-FR-META-COOLCLIP",
    "目标市场": "fr",
    "平台": "Meta",
    "产品品类": "3C数码",
    "目标人群": "18-30岁法国年轻游戏玩家/学生（价格敏感）",
    "品牌要求": "CoolClip：年轻、潮流、游戏向；避免夸大功效和未验证的绝对化宣称",
}

BRAND = {
    "brand_name": "CoolClip",
    "brand_name_rule": "品牌名保持英文原样",
    "protected_terms": [{"term": "CoolClip", "rule": "品牌名保持原样"}],
    "tone": "年轻、潮流、游戏向",
    "do": ["突出快速降温、游戏体验，用符合法语习惯的表达"],
    "avoid": ["未经验证的量化功效宣称", "催促式美式促销语气"],
}


def build_demo_package(runner: Callable[..., Dict[str, Any]] = localize) -> Dict[str, Any]:
    result = runner(SOURCE_BRIEF, "fr", brand=BRAND, verbose=False)
    package = build_creative_package(SOURCE_TASK, result)
    kreado = to_kreado_brief(package["strategy"])
    return {
        "demo_status": "真实 localize() 产出；洞察和策略引用画像条目；非企业真实调研结论",
        "input_brief": SOURCE_BRIEF,
        "input_task": SOURCE_TASK,
        "market": "fr",
        "platform": "Meta",
        "category": "3C数码",
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
