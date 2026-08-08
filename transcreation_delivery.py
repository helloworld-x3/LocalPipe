"""Professional transcreation delivery assembled from real LocalPipe output."""

from __future__ import annotations

from typing import Any, Dict

from language_assets import audit_language_assets
from quality_framework import build_quality_report


def build_transcreation_delivery(
    result: Dict[str, Any],
    route: Dict[str, Any],
    kreado: Dict[str, Any],
    language_assets: Dict[str, Any],
) -> Dict[str, Any]:
    target_copy = str(result.get("copy", "")).strip()
    if not target_copy:
        raise ValueError("transcreation delivery requires target copy")
    quality_report = build_quality_report(result)
    language_asset_audit = audit_language_assets(target_copy, language_assets)
    decision_rank = {"publish": 0, "needs_review": 1, "block": 2}
    final_decision = max(
        (quality_report["release_decision"], language_asset_audit["release_decision"]),
        key=lambda item: decision_rank.get(item, 2),
    )
    return {
        "target_copy": target_copy,
        "back_translation_zh": str(result.get("copy_zh", "")).strip(),
        "creative_rationale": str(result.get("adaptation_note", "")).strip(),
        "route": dict(route),
        "recommended_use": str(route.get("recommended_use", "人工审核后发布")),
        "profile_trace": dict(result.get("profile_trace") or {}),
        "language_assets": language_assets,
        "language_asset_audit": language_asset_audit,
        "quality_report": quality_report,
        "final_delivery_decision": final_decision,
        "kreado_brief": kreado,
    }
