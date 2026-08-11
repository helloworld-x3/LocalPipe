"""Atomic, input-keyed checkpoints for resumable Feishu task execution."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional


def _field(task: Dict[str, Any], name: str, default: Any = "") -> Any:
    if name == "record_id" and "record_id" in task:
        return task.get("record_id", default)
    fields = task.get("fields") if isinstance(task.get("fields"), dict) else task
    aliases = {
        "record_id": ("record_id",), "task_id": ("任务ID", "task_id", "id"),
        "source": ("中文原文", "source", "source_text", "text"),
        "market": ("目标市场", "market", "market_code"),
    }
    for key in aliases.get(name, (name,)):
        if key in fields:
            return fields.get(key, default)
    lowered = {str(k).strip().lower(): k for k in fields}
    for key in aliases.get(name, (name,)):
        if key.lower() in lowered:
            return fields.get(lowered[key.lower()], default)
    return default


def task_key(task: Dict[str, Any]) -> str:
    record_id = str(_field(task, "record_id", "")).strip()
    task_id = str(_field(task, "task_id", "")).strip()
    source = str(_field(task, "source", "")).strip()
    market = str(_field(task, "market", "")).strip().lower()
    raw = json.dumps([record_id, task_id, source, market], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CheckpointStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or os.environ.get("LOCALPIPE_CHECKPOINT_FILE", Path(__file__).resolve().parent / ".cache" / "task_checkpoints.json"))
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def load(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = task_key(task)
        with self._lock:
            return self._read().get(key)

    def save_generated(
        self,
        task: Dict[str, Any],
        result: Dict[str, Any],
        fields: Dict[str, Any],
        run_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = task_key(task)
        checkpoint = {
            "checkpoint_version": "v1", "key": key,
            "record_id": str(_field(task, "record_id", "")).strip(),
            "task_id": str(_field(task, "task_id", "")).strip(),
            "result": result, "fields": fields,
            "run_snapshot": run_snapshot or {},
            "output_written": False, "output_record_id": "",
        }
        with self._lock:
            data = self._read()
            data[key] = checkpoint
            self._write(data)
        return checkpoint

    def mark_output_written(self, task: Dict[str, Any], output_record_id: str) -> None:
        key = task_key(task)
        with self._lock:
            data = self._read()
            checkpoint = data.get(key)
            if not checkpoint:
                return
            checkpoint["output_written"] = True
            checkpoint["output_record_id"] = str(output_record_id or "")
            self._write(data)
