"""Safe evidence-candidate intake for profile calibration.

This module only records candidate evidence. It never edits a formal profile;
human confirmation is required before ``candidate_to_revision`` can be used.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_PATH = Path(__file__).resolve().parent / ".cache" / "evidence_candidates.jsonl"
_LOCK = threading.Lock()


def _hash_payload(payload: Dict[str, Any]) -> str:
    stable = {
        "market_code": payload.get("market_code", ""),
        "entry_type": payload.get("entry_type", ""),
        "candidate_claim": payload.get("candidate_claim", ""),
        "publisher": payload.get("publisher", ""),
        "source_url": payload.get("source_url", ""),
        "quote": payload.get("quote", ""),
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_evidence_candidate(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("证据候选必须是对象")
    market = str(raw.get("market_code") or raw.get("market") or "").strip().lower()
    entry_type = str(raw.get("entry_type") or raw.get("type") or "").strip()
    claim = str(raw.get("candidate_claim") or raw.get("content") or "").strip()
    publisher = str(raw.get("publisher") or "").strip()
    source_url = str(raw.get("source_url") or raw.get("url") or "").strip()
    quote = str(raw.get("quote") or "").strip()
    if not market or not entry_type or not claim:
        raise ValueError("证据候选缺少 market_code/entry_type/candidate_claim")
    if not publisher or not source_url:
        raise ValueError("证据候选必须带 publisher 和 source_url")
    status = str(raw.get("status") or "待确认").strip()
    if status not in {"待确认", "已确认", "已拒绝"}:
        raise ValueError("证据候选 status 必须是待确认/已确认/已拒绝")
    candidate = {
        "schema_version": "evidence-candidate-v1",
        "candidate_id": str(raw.get("candidate_id") or "").strip(),
        "market_code": market,
        "entry_type": entry_type,
        "candidate_claim": claim,
        "publisher": publisher,
        "title": str(raw.get("title") or "").strip(),
        "source_url": source_url,
        "quote": quote,
        "retrieved_at": str(raw.get("retrieved_at") or datetime.now(timezone.utc).date().isoformat()),
        "evidence_level": str(raw.get("evidence_level") or "C").strip().upper(),
        "status": status,
    }
    candidate["content_hash"] = str(raw.get("content_hash") or _hash_payload(candidate))
    if len(candidate["content_hash"]) != 64:
        raise ValueError("content_hash 必须是 SHA-256")
    return candidate


def _read(path: Path) -> list[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_evidence_candidate(candidate: Dict[str, Any], path: Optional[Path] = None) -> bool:
    normalized = normalize_evidence_candidate(candidate)
    target = Path(path) if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        existing = _read(target)
        if any(row.get("content_hash") == normalized["content_hash"] for row in existing):
            return False
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def candidate_to_revision(candidate: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_evidence_candidate(candidate)
    if normalized["status"] != "已确认":
        raise ValueError("只有已确认的证据候选才能进入画像修订")
    return {
        "action": "new",
        "entry_type": normalized["entry_type"],
        "content": normalized["candidate_claim"],
        "confidence": "中",
        "expires": None,
        "reason": (
            f"证据候选已确认；来源：{normalized['publisher']}；"
            f"URL：{normalized['source_url']}；内容哈希：{normalized['content_hash']}"
        ),
    }
