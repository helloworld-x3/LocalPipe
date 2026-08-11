"""Small, explicit evidence-source adapters.

Adapters normalize structured excerpts only; they do not fetch the web and never
promote a candidate beyond the existing human-confirmation gate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from evidence_candidates import append_evidence_candidate, normalize_evidence_candidate


_SOURCE_POLICY = {
    "manual": {"publisher": "", "evidence_level": "C"},
    "ARPP": {"publisher": "ARPP", "evidence_level": "A"},
    "FEVAD": {"publisher": "FEVAD", "evidence_level": "A"},
}


def available_sources() -> List[str]:
    return sorted(_SOURCE_POLICY)


def adapt_evidence(
    source: str,
    raw: Dict[str, Any],
    *,
    publisher: Optional[str] = None,
) -> Dict[str, Any]:
    source_name = str(source or "").strip()
    policy = _SOURCE_POLICY.get(source_name)
    if not policy:
        raise ValueError(f"未知证据来源适配器: {source_name}")
    payload = dict(raw or {})
    chosen_publisher = str(publisher or policy["publisher"] or payload.get("publisher") or "").strip()
    if not chosen_publisher:
        raise ValueError("manual 证据必须提供 publisher")
    payload["publisher"] = chosen_publisher
    payload["source_url"] = str(payload.get("source_url") or payload.get("url") or "").strip()
    payload["evidence_level"] = policy["evidence_level"]
    # An adapter can only enter the review queue; confirmation is human-owned.
    payload["status"] = "待确认"
    return normalize_evidence_candidate(payload)


def append_adapted_evidence(
    source: str,
    raw: Dict[str, Any],
    *,
    publisher: Optional[str] = None,
    path: Optional[str] = None,
) -> bool:
    candidate = adapt_evidence(source, raw, publisher=publisher)
    return append_evidence_candidate(candidate, path=path)
