"""DCO-inspired creative route construction for controlled diversification."""

from __future__ import annotations

from typing import Any, Dict, List


def build_creative_matrix(strategy: Dict[str, Any], count: int = 3) -> List[Dict[str, Any]]:
    """Create differentiated routes from the same evidence-backed strategy."""
    selling_points = [str(x).strip() for x in strategy.get("selling_points") or [] if str(x).strip()]
    if not selling_points:
        raise ValueError("creative matrix requires at least one selling point")
    scene = str(strategy.get("scene_direction") or "真实使用场景").strip()
    tone = str(strategy.get("tone_direction") or "自然、可信").strip()
    hook = str(strategy.get("hook") or "先展示真实问题，再给出产品证据").strip()
    evidence_ids = list(strategy.get("evidence_ids") or [])
    cta = str(strategy.get("cta") or "了解更多").strip()
    platform = str(strategy.get("platform") or "Meta").strip()

    routes = [
        {
            "route_id": "product_proof",
            "objective": "产品证据",
            "primary_selling_point": selling_points[0],
            "hook": f"用材质或动作近景证明{selling_points[0]}",
            "visual_direction": f"在{scene}中使用近景和真实动作展示{selling_points[0]}",
            "tone": tone,
            "recommended_use": f"{platform} 主文案/产品利益版本",
        },
        {
            "route_id": "scene_fit",
            "objective": "场景适配",
            "primary_selling_point": selling_points[min(1, len(selling_points) - 1)],
            "hook": hook,
            "visual_direction": f"以{scene}的连续切换证明穿着适配性",
            "tone": tone,
            "recommended_use": f"{platform} 场景化变体",
        },
        {
            "route_id": "brand_emotion",
            "objective": "品牌情绪",
            "primary_selling_point": selling_points[-1],
            "hook": f"从用户希望获得的{tone}体验切入",
            "visual_direction": f"以克制的都市生活镜头呈现{selling_points[-1]}，结尾引导{cta}",
            "tone": tone,
            "recommended_use": f"{platform} 品牌心智变体",
        },
    ]
    for route in routes:
        route["evidence_ids"] = evidence_ids
        route["platform"] = platform
    return routes[: max(1, min(int(count), len(routes)))]
