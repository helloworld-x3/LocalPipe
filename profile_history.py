"""Immutable profile snapshots, provenance manifest, and atomic rollback."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


BASE_DIR = Path(__file__).resolve().parent
_LOCK = threading.Lock()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _version(profile: Dict[str, Any]) -> str:
    return str(profile.get("version") or "v0.0").strip()


class ProfileHistory:
    """Append-only profile history; active profile files remain the source of truth."""

    def __init__(self, history_dir: Optional[Path | str] = None):
        self.history_dir = Path(
            history_dir
            or os.environ.get("LOCALPIPE_PROFILE_HISTORY_DIR", BASE_DIR / "profiles" / "history")
        )
        self.manifest_path = self.history_dir / "manifest.jsonl"

    def _append(self, record: Dict[str, Any]) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def snapshot_before_update(
        self,
        profile: Dict[str, Any],
        *,
        market_code: str,
        profile_path: Path | str,
        source: str = "",
        review_record_ids: Optional[Iterable[str]] = None,
    ) -> Path:
        """Persist the exact pre-update JSON once; never overwrite an existing snapshot."""
        market = str(market_code or profile.get("market_code") or "").strip().lower()
        if not market:
            raise ValueError("profile history requires market_code")
        path = Path(profile_path)
        if path.is_file():
            file_raw = path.read_text(encoding="utf-8")
            try:
                file_profile = json.loads(file_raw)
            except ValueError:
                file_profile = None
            raw = file_raw if file_profile == profile else _canonical_json(profile)
        else:
            raw = _canonical_json(profile)
        target = self.history_dir / f"{market}-{_version(profile)}.json"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_text(raw, encoding="utf-8")
            os.replace(temp, target)
        return target

    def record_update(
        self,
        snapshot_path: Path | str,
        before: Dict[str, Any],
        after: Dict[str, Any],
        *,
        source: str = "",
        review_record_ids: Optional[Iterable[str]] = None,
        revision_record_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        before_text = _canonical_json(before)
        after_text = _canonical_json(after)
        market = str(after.get("market_code") or before.get("market_code") or "").strip().lower()
        record = {
            "operation": "update",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "market_code": market,
            "snapshot": Path(snapshot_path).name,
            "before_version": _version(before),
            "after_version": _version(after),
            "before_hash": _sha256_text(before_text),
            "after_hash": _sha256_text(after_text),
            "source": str(source or ""),
            "review_record_ids": [str(item) for item in (review_record_ids or []) if str(item).strip()],
            "revision_record_ids": [str(item) for item in (revision_record_ids or []) if str(item).strip()],
            "diff": "".join(difflib.unified_diff(
                before_text.splitlines(True), after_text.splitlines(True),
                fromfile=f"{market}-{_version(before)}.json",
                tofile=f"{market}-{_version(after)}.json",
            )),
        }
        self._append(record)
        return record

    def rollback(self, market_code: str, version: str, *, profile_path: Optional[Path | str] = None) -> Dict[str, Any]:
        market = str(market_code or "").strip().lower()
        target = self.history_dir / f"{market}-{str(version).strip()}.json"
        if not target.is_file():
            raise FileNotFoundError(f"没有历史画像快照: {target}")
        restored_text = target.read_text(encoding="utf-8")
        restored = json.loads(restored_text)
        active_path = Path(profile_path or BASE_DIR / "profiles" / f"{market}.json")
        current = json.loads(active_path.read_text(encoding="utf-8")) if active_path.is_file() else {}
        if current:
            current_snapshot = self.snapshot_before_update(
                current,
                market_code=market,
                profile_path=active_path,
                source="manual rollback pre-image",
            )
        else:
            current_snapshot = None
        active_path.parent.mkdir(parents=True, exist_ok=True)
        temp = active_path.with_suffix(active_path.suffix + ".tmp")
        temp.write_text(restored_text, encoding="utf-8")
        os.replace(temp, active_path)
        self._append({
            "operation": "rollback",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "market_code": market,
            "snapshot": target.name,
            "pre_rollback_snapshot": current_snapshot.name if current_snapshot else "",
            "current_version": _version(current),
            "restored_version": _version(restored),
            "current_hash": _sha256_text(_canonical_json(current)) if current else "",
            "restored_hash": _sha256_text(_canonical_json(restored)),
            "source": "manual rollback",
        })
        return restored


def rollback_profile(
    market_code: str,
    version: str,
    *,
    profile_path: Optional[Path | str] = None,
    history_dir: Optional[Path | str] = None,
) -> Dict[str, Any]:
    return ProfileHistory(history_dir).rollback(market_code, version, profile_path=profile_path)
