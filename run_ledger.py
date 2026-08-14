"""Reproducible, secret-free run snapshots and append-only local ledger."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from market_code import validate_market_code


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LEDGER_PATH = BASE_DIR / ".cache" / "run_ledger.jsonl"
_LEDGER_LOCK = threading.Lock()


def _field(record: Dict[str, Any], name: str, default: Any = "") -> Any:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    aliases = {
        "task_id": ("任务ID", "task_id", "id"),
        "source": ("中文原文", "source", "source_text", "text"),
        "market": ("目标市场", "market", "market_code"),
        "platform": ("平台", "platform"),
        "category": ("产品品类", "category"),
    }
    lowered = {str(key).strip().lower(): key for key in fields}
    for candidate in aliases.get(name, (name,)):
        actual = lowered.get(str(candidate).strip().lower())
        if actual is not None:
            return fields.get(actual, default)
    return default


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _profile_hash(market_code: str) -> str:
    market_code = str(market_code or "").strip().lower()
    if not market_code:
        return ""
    market_code = validate_market_code(market_code)
    path = BASE_DIR / "profiles" / f"{market_code}.json"
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_run_snapshot(
    task: Dict[str, Any],
    result: Dict[str, Any],
    *,
    quality_decision: str,
    strategy: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an audit snapshot without API keys, tokens or raw credentials."""
    strategy = strategy or {}
    source = str(_field(task, "source", result.get("source_text", "")) or "").strip()
    market = str(_field(task, "market", strategy.get("market", "")) or "").strip().lower()
    output = {
        "copy": result.get("copy", ""),
        "copy_zh": result.get("copy_zh", ""),
        "final_status": result.get("final_status", "error"),
        "fidelity": result.get("fidelity") or {},
        "taboo": result.get("taboo") or {},
        "profile_trace": result.get("profile_trace") or {},
    }
    return {
        "schema_version": "run-snapshot-v1",
        "run_id": run_id or f"run_{uuid.uuid4().hex}",
        "created_at": created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "task_id": str(_field(task, "task_id", "")).strip(),
        "market": market,
        "platform": str(_field(task, "platform", strategy.get("platform", "")) or "").strip(),
        "category": str(_field(task, "category", "") or "").strip(),
        "model": os.environ.get("LLM_MODEL", "deepseek-v4-pro"),
        "source_hash": _canonical_hash(source),
        "output_hash": _canonical_hash(output),
        "profile_version": str(result.get("profile_version") or strategy.get("profile_version") or ""),
        "profile_hash": _profile_hash(market),
        "used_entries": list(result.get("used_entries") or []),
        "directive_trace": dict(strategy.get("directive_trace") or {}),
        "pipeline_status": str(result.get("final_status", "error")),
        "quality_decision": str(quality_decision or "needs_review"),
        "fidelity_rate": float((result.get("fidelity") or {}).get("recovery_rate", 0.0) or 0.0),
        "taboo_risk": str((result.get("taboo") or {}).get("risk_level", "unknown")),
        "errors": list(result.get("errors") or []),
    }


def append_run_snapshot(snapshot: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Append one snapshot as JSONL; never rewrite previous ledger entries."""
    target = Path(path) if path is not None else Path(os.environ.get("LOCALPIPE_RUN_LEDGER", DEFAULT_LEDGER_PATH))
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    with _LEDGER_LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return target
