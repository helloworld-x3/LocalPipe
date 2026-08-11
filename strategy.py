"""Auditable market/platform creative strategy assembly.

Market conclusions are supplied by ``profile_insights``. This module keeps
only platform execution conventions and combines them with the source brief.
"""

from __future__ import annotations

from typing import Any, Dict, List

from profile_insights import load_profile_summary
from strategy_compiler import compile_execution_directives, compact_text


PLATFORM_RULES = {
    "Meta": {
        "hook": "前三秒先展示问题、真实使用结果或关键细节，再给出产品解决方案",
        "format": "短句、清晰视觉层级、单一行动号召",
    },
    "TikTok": {
        "hook": "前三秒用反差、真人反应或快速结果抓住注意力",
        "format": "口语化、节奏快、适合评论互动",
    },
}


def _require_text(data: Dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"策略输入缺少 {key}")
    return value


def build_strategy(brief: Dict[str, Any]) -> Dict[str, Any]:
    market = _require_text(brief, "market").lower()
    platform = _require_text(brief, "platform")
    audience = _require_text(brief, "audience")
    selling_points = brief.get("selling_points") or []
    if not isinstance(selling_points, list) or not selling_points or not all(str(x).strip() for x in selling_points):
        raise ValueError("策略输入 selling_points 必须是非空字符串列表")
    cta = _require_text(brief, "cta")

    profile_summary = brief.get("profile_summary")
    if not isinstance(profile_summary, dict):
        profile_summary = load_profile_summary(
            market,
            category=str(brief.get("category", "")),
            platform=platform,
        )
    platform_rule = PLATFORM_RULES.get(platform, {
        "hook": "先展示问题或真实使用场景，再展示解决方案",
        "format": "短句、单一卖点、明确行动号召",
    })
    primary = str(selling_points[0])
    directives, directive_trace = compile_execution_directives(
        profile_summary,
        platform_fallback=platform_rule["format"],
    )
    scene = directives["scene"]
    requested_visual = str(brief.get("visual_direction") or "").strip()
    if "基于画像条目" in requested_visual or "画像条目" in requested_visual:
        requested_visual = ""
    visual_direction = compact_text(requested_visual or directives["visual"], max_chars=120) or directives["visual"]
    directives = {
        **directives,
        "hook": platform_rule["hook"],
        "visual": visual_direction,
    }
    angles: List[str] = [
        f"结果先行：用{primary}配合“{platform_rule['hook']}”建立第一印象",
        f"场景证明：根据画像证据，在{scene}中展示{primary}的实际价值",
    ]
    if len(selling_points) > 1:
        angles.append(f"卖点递进：先证明{primary}，再补充{selling_points[1]}，最后引导{cta}")
    return {
        "market": market,
        "platform": platform,
        "audience": audience,
        "primary_selling_point": primary,
        "selling_points": [str(x) for x in selling_points],
        "selling_points_order": [str(x) for x in selling_points],
        "hook": platform_rule["hook"],
        "format_direction": platform_rule["format"],
        "scene_direction": scene,
        "tone_direction": directives["tone"],
        "creative_angles": angles,
        "cta": cta,
        "copy": str(brief.get("copy", "")).strip(),
        "visual_direction": visual_direction,
        "risk_notes": directives["avoid"][0],
        "profile_version": profile_summary.get("profile_version", ""),
        "execution_directives": directives,
        "directive_trace": directive_trace,
        "evidence_ids": list(profile_summary.get("evidence_ids") or []),
        "evidence": list(profile_summary.get("evidence") or []),
        "risk_evidence_ids": list(profile_summary.get("risk_evidence_ids") or []),
        "confidence": float(profile_summary.get("confidence", 0.0)),
        "evidence_levels": list(profile_summary.get("evidence_levels") or []),
        "evidence_details": list(profile_summary.get("evidence_details") or profile_summary.get("evidence") or []),
        "publisher": profile_summary.get("publisher", "LocalPipe research draft"),
        "evidence_level": profile_summary.get("evidence_level", "C"),
        "source_urls": list(profile_summary.get("source_urls") or []),
        "validation_status": profile_summary.get("validation_status", "待人工复核"),
        "unverified_claims": list(profile_summary.get("unverified_claims") or []),
    }
