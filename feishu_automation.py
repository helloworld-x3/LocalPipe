"""HTTP bridge for Feishu Automation -> LocalPipe task execution.

Feishu Automation sends a small JSON payload containing a Bitable task
``record_id``.  This bridge acknowledges quickly, then runs the existing
connector in a background thread.  It intentionally uses only the Python
standard library; LocalPipe remains responsible for generation and quality
control, while Feishu remains the task/review workspace.

Run locally or behind a company-approved HTTPS reverse proxy:
    python feishu_automation.py --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping, Optional

from feishu_connector import (
    FIELD_STATUS,
    FeishuBitableClient,
    _make_client,
    complete_review,
    query_task_summary,
    run_live,
    sync_metrics_snapshot,
)


def task_waiting_for_generation(record_id: str) -> bool:
    """Return whether an existing task was explicitly reset for regeneration."""
    record_id = str(record_id or "").strip()
    if not record_id:
        return False
    try:
        task = next(
            (row for row in _make_client().list_tasks() if str(row.get("record_id", "")).strip() == record_id),
            None,
        )
    except Exception:
        return False
    fields = (task or {}).get("fields") or {}
    return str(fields.get(FIELD_STATUS, "")).strip() == "待生成"


def mark_generation_failed(record_id: str, error_type: str) -> None:
    """Expose background failures in the task table without leaking details."""
    _make_client().update_task(str(record_id), {
        FIELD_STATUS: "异常",
        "当前阶段": "异常",
        "异常摘要": f"自动生成失败（{str(error_type or 'Error')}），请重新提交",
    })


def extract_record_id(payload: Any) -> str:
    """Extract a Bitable record ID from common Feishu Automation payloads."""
    if not isinstance(payload, dict):
        return ""
    direct_keys = ("record_id", "recordId", "task_record_id", "taskRecordId")
    for key in direct_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container_key in ("data", "event", "body", "payload"):
        nested = payload.get(container_key)
        record_id = extract_record_id(nested)
        if record_id:
            return record_id
    return ""


def extract_task_id(payload: Any) -> str:
    """Extract a business task ID from common Aily/Feishu payload shapes."""
    if not isinstance(payload, dict):
        return ""
    for key in ("task_id", "taskId", "任务ID"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container_key in ("data", "event", "body", "payload", "arguments", "params"):
        task_id = extract_task_id(payload.get(container_key))
        if task_id:
            return task_id
    return ""


def extract_challenge(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("challenge"), str):
        return payload["challenge"]
    return ""


def extract_action(payload: Any) -> str:
    """Extract a safe Feishu command; legacy payloads default to generation."""
    if not isinstance(payload, dict):
        return "generate"
    value = payload.get("action")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for container_key in ("data", "event", "body", "payload"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            action = extract_action(nested)
            if action != "generate" or "action" in nested:
                return action
    return "generate"


def provided_token(headers: Mapping[str, str], payload: Any = None) -> str:
    """Read the bridge token from headers or an optional payload field."""
    for name in ("X-LocalPipe-Token", "X-Feishu-Token", "X-Webhook-Token"):
        value = headers.get(name, "")
        if value:
            return value.strip()
    authorization = headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if isinstance(payload, dict):
        value = payload.get("token")
        if isinstance(value, str):
            return value.strip()
    return ""


MAX_BODY_BYTES = 64 * 1024


def parse_automation_payload(raw_body: bytes) -> Dict[str, Any]:
    """Parse a normal JSON payload or Feishu's unquoted record-ID template.

    Bitable automation sometimes renders a dynamic ``record_id`` value as a
    raw token inside the JSON editor.  Accept only that narrow, generated
    shape; all other malformed request bodies remain rejected.
    """
    text = raw_body.decode("utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.fullmatch(
            r'\s*\{\s*"action"\s*:\s*"generate"\s*,\s*'
            r'"record_id"\s*:\s*"?(rec[A-Za-z0-9_-]{4,})"?\s*\}\s*',
            text,
        )
        if not match:
            raise
        payload = {"action": "generate", "record_id": match.group(1)}
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("payload must be an object", text, 0)
    return payload
COMPLETED_TTL = 24 * 3600
MAX_CONCURRENT_TASKS = 2
DEFAULT_EVENT_LEDGER = Path(__file__).resolve().parent / ".cache" / "feishu_automation_events.jsonl"
_EVENT_LOCK = threading.Lock()


def append_automation_event(event: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = Path(path or os.environ.get("FEISHU_AUTOMATION_LEDGER", DEFAULT_EVENT_LEDGER))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    with _EVENT_LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return target


def append_feishu_event(event: Dict[str, Any]) -> str:
    """Mirror a sanitized automation event into the optional Feishu event table."""
    event_table = os.environ.get("FEISHU_EVENT_TABLE_ID", "")
    if not event_table:
        return ""
    app_token = os.environ.get("FEISHU_APP_TOKEN", "")
    task_table = os.environ.get("FEISHU_TASK_TABLE_ID", "")
    output_table = os.environ.get("FEISHU_OUTPUT_TABLE_ID", "")
    output_app = os.environ.get("FEISHU_OUTPUT_APP_TOKEN", app_token)
    event_app = os.environ.get("FEISHU_EVENT_APP_TOKEN", app_token)
    if not all((app_token, task_table, output_table)):
        return ""
    client = FeishuBitableClient(
        app_token,
        task_table,
        output_table,
        output_app,
        event_table=event_table,
        event_app_token=event_app,
    )
    occurred_at = int(time.time() * 1000)
    fields = {
        "任务记录ID": str(event.get("record_id", "")),
        "事件类型": str(event.get("event", "")),
        "耗时秒": round(float(event.get("duration_ms", 0) or 0) / 1000.0, 3),
        "错误类型": str(event.get("error_type", "")),
        "说明": {
            "queued": "任务进入 LocalPipe 执行队列",
            "completed": "LocalPipe 执行完成并写回飞书",
            "duplicate": "重复触发已被幂等保护拦截",
            "failed": "后台执行失败，未写入原始异常详情",
        }.get(str(event.get("event", "")), ""),
        "发生时间": occurred_at,
    }
    event_id = client.create_record(event_table, fields, event_app)
    if event_id:
        client.update_record(event_table, event_id, {"事件ID": event_id}, event_app)
    return event_id


def build_live_event_logger(
    local_logger: Callable[[Dict[str, Any]], Any] = append_automation_event,
    feishu_logger: Callable[[Dict[str, Any]], Any] = append_feishu_event,
) -> Callable[[Dict[str, Any]], None]:
    """Keep local evidence complete while only sending a safe subset to Feishu."""
    def log(event: Dict[str, Any]) -> None:
        local_logger(event)
        sanitized = {
            key: event[key]
            for key in ("event", "action", "record_id", "duration_ms", "error_type")
            if key in event
        }
        try:
            feishu_logger(sanitized)
        except Exception:
            pass

    return log


class AutomationService:
    """Deduplicating asynchronous dispatcher for automation callbacks."""

    def __init__(
        self,
        runner: Optional[Callable[[str], Any]] = None,
        review_runner: Optional[Callable[[str], Any]] = None,
        metrics_runner: Optional[Callable[[], Any]] = None,
        query_runner: Optional[Callable[[str], Dict[str, Any]]] = None,
        event_logger: Optional[Callable[[Dict[str, Any]], Any]] = None,
        retry_checker: Optional[Callable[[str], bool]] = None,
        failure_handler: Optional[Callable[[str, str], Any]] = None,
    ):
        uses_live_runner = runner is None
        self.runner = runner or (lambda record_id: run_live(task_record_id=record_id))
        self.review_runner = review_runner or complete_review
        self.metrics_runner = metrics_runner or sync_metrics_snapshot
        self.query_runner = query_runner or (lambda task_id: query_task_summary(_make_client(), task_id))
        self.event_logger = event_logger or (build_live_event_logger() if uses_live_runner else (lambda event: None))
        self.retry_checker = retry_checker or (task_waiting_for_generation if uses_live_runner else None)
        self.failure_handler = failure_handler or (mark_generation_failed if uses_live_runner else None)
        self._active = set()
        self._completed: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_TASKS)
        self.last_error: Optional[str] = None

    def _log_event(self, event: Dict[str, Any]) -> None:
        try:
            self.event_logger(event)
        except Exception:
            pass

    def submit(self, record_id: str) -> Dict[str, Any]:
        return self.submit_action("generate", record_id)

    def query(self, task_id: str) -> Dict[str, Any]:
        """Synchronously read an existing result without entering the write queue."""
        return self.query_runner(str(task_id or "").strip())

    def submit_action(self, action: str, record_id: str = "") -> Dict[str, Any]:
        action = str(action or "generate").strip()
        if action not in {"generate", "complete_review", "sync_metrics"}:
            return {"accepted": False, "status": "invalid_action", "error": "unsupported action"}
        record_id = str(record_id or "").strip()
        if action != "sync_metrics" and not record_id:
            return {"accepted": False, "status": "invalid", "error": "missing record_id"}
        dedupe_key = record_id if action == "generate" else f"{action}:{record_id or 'global'}"
        with self._lock:
            now = time.time()
            for rid in [r for r, ts in self._completed.items() if now - ts > COMPLETED_TTL]:
                self._completed.pop(rid, None)
            retry_requested = (
                action == "generate"
                and dedupe_key in self._completed
                and self.retry_checker is not None
                and self.retry_checker(record_id)
            )
            if retry_requested:
                self._completed.pop(dedupe_key, None)
            if dedupe_key in self._active or dedupe_key in self._completed:
                self._log_event({"event": "duplicate", "action": action, "record_id": record_id})
                return {"accepted": True, "status": "duplicate", "action": action, "record_id": record_id}
            self._active.add(dedupe_key)
        self._log_event({"event": "queued", "action": action, "record_id": record_id})
        thread = threading.Thread(target=self._execute, args=(action, record_id, dedupe_key), daemon=True)
        thread.start()
        return {"accepted": True, "status": "queued", "action": action, "record_id": record_id}

    def _execute(self, action: str, record_id: str, dedupe_key: str) -> None:
        started = time.monotonic()
        try:
            with self._semaphore:
                if action == "generate":
                    self.runner(record_id)
                elif action == "complete_review":
                    self.review_runner(record_id)
                else:
                    self.metrics_runner()
            with self._lock:
                self._completed[dedupe_key] = time.time()
            self._log_event({
                "event": "completed",
                "action": action,
                "record_id": record_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
            })
        except Exception as exc:  # pragma: no cover - surfaced through logs/health
            self.last_error = f"{type(exc).__name__}: {exc}"
            if action == "generate" and self.failure_handler is not None:
                try:
                    self.failure_handler(record_id, type(exc).__name__)
                except Exception:
                    pass
            self._log_event({
                "event": "failed",
                "action": action,
                "record_id": record_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
            })
        finally:
            with self._lock:
                self._active.discard(dedupe_key)


class FeishuAutomationHandler(BaseHTTPRequestHandler):
    server_version = "LocalPipeFeishuAutomation/1.0"

    _challenge_lock = threading.Lock()
    _challenge_times: Dict[str, float] = {}
    _challenge_window = 60.0
    _challenge_max = 10

    def _challenge_allowed(self) -> bool:
        ip = self.address_string()
        now = time.time()
        with self._challenge_lock:
            # 惰性清理窗口期外的 IP 记录，避免长期运行内存缓慢增长
            stale = [
                key for key, ts in self._challenge_times.items()
                if now - ts > self._challenge_window
            ]
            for key in stale:
                self._challenge_times.pop(key, None)
            last = self._challenge_times.get(ip, 0.0)
            if now - last < self._challenge_window / self._challenge_max:
                return False
            self._challenge_times[ip] = now
            return True

    @property
    def service(self) -> AutomationService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def expected_token(self) -> str:
        return self.server.expected_token  # type: ignore[attr-defined]

    def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._write_json(HTTPStatus.OK, {"ok": True, "service": "localpipe-feishu-automation"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in ("", "/trigger", "/webhook", "/query"):
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid Content-Length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload too large"})
            return
        try:
            payload = parse_automation_payload(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON"})
            return

        challenge = extract_challenge(payload)
        if challenge:
            if not self._challenge_allowed():
                self._write_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "too many requests"})
                return
            self._write_json(HTTPStatus.OK, {"challenge": challenge})
            return
        if not self.expected_token:
            self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "server not configured with a token"})
            return
        provided = provided_token(self.headers, payload)
        if not hmac.compare_digest(provided.encode("utf-8"), str(self.expected_token).encode("utf-8")):
            self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid token"})
            return

        if path == "/query":
            task_id = extract_task_id(payload)
            if not task_id:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing task_id"})
                return
            try:
                response = self.service.query(task_id)
            except LookupError:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "task not found"})
                return
            except Exception:
                self._write_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "query failed"})
                return
            self._write_json(HTTPStatus.OK, response)
            return

        record_id = extract_record_id(payload)
        response = self.service.submit_action(extract_action(payload), record_id)
        self._write_json(HTTPStatus.ACCEPTED if response.get("accepted") else HTTPStatus.BAD_REQUEST, response)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[feishu-automation] {self.address_string()} - {format % args}")


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    service: Optional[AutomationService] = None,
    expected_token: Optional[str] = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, int(port)), FeishuAutomationHandler)
    server.service = service or AutomationService()  # type: ignore[attr-defined]
    server.expected_token = expected_token if expected_token is not None else os.environ.get("FEISHU_AUTOMATION_TOKEN", "")  # type: ignore[attr-defined]
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu Automation HTTP bridge for LocalPipe")
    parser.add_argument("--host", default=os.environ.get("LOCALPIPE_AUTOMATION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOCALPIPE_AUTOMATION_PORT", "8080")))
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"LocalPipe 飞书自动化桥接已启动: http://{args.host}:{args.port}/trigger")
    print("只读查询: /query；健康检查: /health；生产环境请通过 HTTPS 反向代理暴露，并配置 FEISHU_AUTOMATION_TOKEN")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
