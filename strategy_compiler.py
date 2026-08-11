"""Compile traceable profile evidence into short execution directives.

Profile entries are evidence and audit material.  Downstream creative tools
need a compact execution contract instead of the full source text.  This
module keeps those two concerns separate while retaining entry IDs for trace.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple


TONE_TYPES = {"语言风格"}
VISUAL_TYPES = {"审美偏好", "消费习惯", "流行元素"}
PLATFORM_TYPES = {"平台特点"}


def compact_text(value: Any, max_chars: int = 96) -> str:
    """Keep the first useful evidence clauses within a prompt-safe length."""
    text = " ".join(str(value or "").split()).strip(" ；;，,。.")
    if not text:
        return ""
    clauses = [part.strip(" ；;，,。.") for part in re.split(r"[；;。\n]+", text) if part.strip()]
    selected = clauses[0] if clauses else text
    if len(selected) <= max_chars:
        return selected
    return selected[: max_chars - 1].rstrip(" ，,；;") + "…"


def _entries(summary: Dict[str, Any], key: str, types: Iterable[str]) -> List[Dict[str, Any]]:
    allowed = set(types)
    result = []
    for item in summary.get(key) or []:
        if isinstance(item, dict) and item.get("id") and item.get("type") in allowed:
            result.append(item)
    return result


def _first_text(items: List[Dict[str, Any]], fallback: Any, max_chars: int = 96) -> str:
    if items:
        return compact_text(items[0].get("content"), max_chars=max_chars)
    return compact_text(fallback, max_chars=max_chars)


def _ids(items: List[Dict[str, Any]]) -> List[str]:
    return [str(item["id"]) for item in items if item.get("id")]


def _confidence(items: List[Dict[str, Any]], fallback: Any = 0.0) -> float:
    values = []
    for item in items:
        value = item.get("confidence")
        if isinstance(value, (int, float)):
            values.append(max(0.0, min(1.0, float(value))))
    if values:
        return round(sum(values) / len(values), 4)
    try:
        return round(max(0.0, min(1.0, float(fallback))), 4)
    except (TypeError, ValueError):
        return 0.0


def compile_execution_directives(
    profile_summary: Dict[str, Any],
    *,
    platform_fallback: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return compact directives and the profile-entry trace behind them."""
    evidence = list(profile_summary.get("evidence") or [])
    risk = list(profile_summary.get("risk_evidence") or [])
    tone_items = _entries({"items": evidence}, "items", TONE_TYPES)[:1]
    if not tone_items:
        tone_items = _entries({"items": evidence}, "items", {"审美偏好"})[:1]
    visual_items = _entries({"items": evidence}, "items", VISUAL_TYPES)[:1]
    platform_items = _entries({"items": evidence}, "items", PLATFORM_TYPES)[:1]

    tone = _first_text(tone_items, profile_summary.get("tone")) or "自然、可信"
    scene = _first_text(visual_items, profile_summary.get("scene")) or "真实使用场景"
    platform = compact_text(platform_fallback, max_chars=96) or _first_text(
        platform_items, profile_summary.get("platform_preference")
    ) or "短句、清晰视觉层级、单一行动号召"
    avoid = _first_text(risk[:2], profile_summary.get("risk_notes"), max_chars=120) or "避免未经证实的承诺，需人工复核"

    directives = {
        "tone": tone,
        "scene": scene,
        "visual": scene,
        "platform": platform,
        "avoid": [avoid],
    }
    trace = {
        "tone_ids": _ids(tone_items),
        "scene_ids": _ids(visual_items),
        "visual_ids": _ids(visual_items),
        # Platform formatting is an execution convention supplied by the
        # adapter; profile platform entries remain available as audit context.
        "platform_ids": [] if platform_fallback else _ids(platform_items),
        "platform_profile_ids": _ids(platform_items),
        "risk_ids": _ids(risk[:2]),
        "confidence": {
            "tone": _confidence(tone_items, profile_summary.get("confidence")),
            "visual": _confidence(visual_items, profile_summary.get("confidence")),
            "platform": _confidence(platform_items, profile_summary.get("confidence")),
            "risk": _confidence(risk[:2], profile_summary.get("confidence")),
        },
    }
    return directives, trace
