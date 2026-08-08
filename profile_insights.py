"""Profile-derived market insight helpers.

This module never invents a market conclusion. It only summarizes the active
profile entries, keeps their IDs and source metadata, and exposes the summary
to the strategy and Feishu layers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


BASE_DIR = Path(__file__).resolve().parent
CONFIDENCE_LABELS = {"高": 0.9, "中": 0.7, "低": 0.4, "high": 0.9, "medium": 0.7, "low": 0.4}
POSITIVE_TYPES = {"语言风格", "审美偏好", "消费习惯", "平台特点", "流行元素", "市场趋势"}


def _confidence_value(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return CONFIDENCE_LABELS.get(str(value).strip().lower(), 0.0)


def _load_profile(market_code: str) -> Dict[str, Any]:
    """Load through LocalPipe's existing integrity/expiry checks."""
    try:
        from pipeline import load_profile

        return load_profile(market_code)
    except ImportError:
        profile_dir = BASE_DIR / "profiles"
        for path in profile_dir.glob("*.json"):
            if path.name.startswith("."):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("market_code") == market_code:
                return data
        raise FileNotFoundError(f"没有 {market_code} 的画像文件")


def _details(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for entry in entries:
        evidence = [item for item in (entry.get("evidence") or []) if isinstance(item, dict)]
        publishers = list(dict.fromkeys(str(item.get("publisher", "")).strip() for item in evidence if item.get("publisher")))
        source_urls = list(dict.fromkeys(str(item.get("url", "")).strip() for item in evidence if item.get("url")))
        evidence_levels = list(dict.fromkeys(str(item.get("evidence_level", "")).strip() for item in evidence if item.get("evidence_level")))
        validation_status = entry.get("validation_status") or ("待法语母语者校准" if str(entry.get("source", "")).startswith("LLM冷启动") else "待人工复核")
        result.append({
            "id": entry.get("id", ""),
            "type": entry.get("type", ""),
            "content": str(entry.get("content", "")).strip(),
            "confidence": _confidence_value(entry.get("confidence")),
            "confidence_label": entry.get("confidence", ""),
            "source": entry.get("source", ""),
            "expires": entry.get("expires"),
            "evidence_details": evidence,
            "evidence_level": evidence_levels[0] if len(evidence_levels) == 1 else (evidence_levels or ["C"])[0],
            "publisher": publishers[0] if len(publishers) == 1 else (publishers or ["LocalPipe research draft"])[0],
            "source_urls": source_urls,
            "validation_status": validation_status,
            "unverified_claims": list(entry.get("unverified_claims") or []),
        })
    return result


def _join_content(entries: Iterable[Dict[str, Any]], fallback: str = "") -> str:
    parts = [str(entry.get("content", "")).strip() for entry in entries if entry.get("content")]
    return "；".join(parts) if parts else fallback


def load_profile_summary(
    market_code: str,
    *,
    category: str = "",
    platform: str = "",
) -> Dict[str, Any]:
    """Return a traceable summary derived from one market profile.

    Positive evidence excludes cultural-taboo entries because those entries
    are controls, not creative references. Risk evidence is kept separately.
    Confidence is the arithmetic mean of the selected entries' metadata.
    """
    market_code = str(market_code or "").strip().lower()
    if not market_code:
        raise ValueError("目标市场不能为空")
    profile = _load_profile(market_code)
    entries = [entry for entry in profile.get("entries", []) if isinstance(entry, dict)]
    positive = [entry for entry in entries if entry.get("type") in POSITIVE_TYPES]
    risk = [entry for entry in entries if entry.get("type") == "文化禁忌"]
    evidence = _details(positive)
    risk_evidence = _details(risk)
    values = [item["confidence"] for item in evidence if item["confidence"] > 0]
    confidence = round(sum(values) / len(values), 4) if values else 0.0

    tone_entries = [entry for entry in positive if entry.get("type") in {"语言风格", "审美偏好"}]
    scene_entries = [entry for entry in positive if entry.get("type") in {"审美偏好", "消费习惯", "流行元素"}]
    platform_entries = [entry for entry in positive if entry.get("type") == "平台特点"]
    audience_entries = [entry for entry in positive if entry.get("type") in {"消费习惯", "审美偏好"}]

    return {
        "market_code": market_code,
        "market": profile.get("market", market_code),
        "language": profile.get("language", ""),
        "profile_version": profile.get("version", ""),
        "category": category,
        "platform": platform,
        "tone": _join_content(tone_entries, "未找到语言风格或审美偏好条目"),
        "scene": _join_content(scene_entries, "未找到场景相关画像条目"),
        "audience_pain_points": _join_content(audience_entries, "未找到受众相关画像条目"),
        "platform_preference": _join_content(platform_entries, "未找到平台特点条目"),
        "risk_notes": _join_content(risk, "未找到文化禁忌条目，仍需人工复核"),
        "evidence_ids": [item["id"] for item in evidence if item["id"]],
        "evidence": evidence,
        "risk_evidence_ids": [item["id"] for item in risk_evidence if item["id"]],
        "risk_evidence": risk_evidence,
        "evidence_sources": list(dict.fromkeys(item["source"] for item in evidence if item["source"])),
        "evidence_details": evidence,
        "publisher": next((item.get("publisher") for item in [*evidence, *risk_evidence] if item.get("publisher")), "LocalPipe research draft"),
        "evidence_level": next((item.get("evidence_level") for item in [*evidence, *risk_evidence] if item.get("evidence_level")), "C"),
        "source_urls": list(dict.fromkeys(
            url for item in [*evidence, *risk_evidence] for url in item.get("source_urls", []) if url
        )),
        "risk_source_urls": list(dict.fromkeys(
            url for item in risk_evidence for url in item.get("source_urls", []) if url
        )),
        "evidence_levels": list(dict.fromkeys(item.get("evidence_level", "") for item in evidence if item.get("evidence_level"))),
        "validation_status": "已公开证据与冷启动假设混合，待母语者校准",
        "unverified_claims": [claim for item in evidence for claim in item.get("unverified_claims", [])],
        "confidence": confidence,
        "expired_ids": list(profile.get("_expired_ids", [])),
    }


def evidence_text(summary: Optional[Dict[str, Any]]) -> str:
    """Compact evidence text for prompts and audit displays."""
    if not summary:
        return "无画像证据"
    return "；".join(f"[{item['id']}] {item['content']}" for item in summary.get("evidence", []))
