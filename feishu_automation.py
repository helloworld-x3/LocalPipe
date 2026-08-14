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
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping, Optional

from feishu_connector import run_live


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


def extract_challenge(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("challenge"), str):
        return payload["challenge"]
    return ""


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


class AutomationService:
    """Deduplicating asynchronous dispatcher for automation callbacks."""

    def __init__(
        self,
        runner: Optional[Callable[[str], Any]] = None,
        event_logger: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        uses_live_runner = runner is None
        self.runner = runner or (lambda record_id: run_live(task_record_id=record_id))
        self.event_logger = event_logger or (append_automation_event if uses_live_runner else (lambda event: None))
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
        record_id = str(record_id or "").strip()
        if not record_id:
            return {"accepted": False, "status": "invalid", "error": "missing record_id"}
        with self._lock:
            now = time.time()
            for rid in [r for r, ts in self._completed.items() if now - ts > COMPLETED_TTL]:
                self._completed.pop(rid, None)
            if record_id in self._active or record_id in self._completed:
                self._log_event({"event": "duplicate", "record_id": record_id})
                return {"accepted": True, "status": "duplicate", "record_id": record_id}
            self._active.add(record_id)
        self._log_event({"event": "queued", "record_id": record_id})
        thread = threading.Thread(target=self._execute, args=(record_id,), daemon=True)
        thread.start()
        return {"accepted": True, "status": "queued", "record_id": record_id}

    def _execute(self, record_id: str) -> None:
        started = time.monotonic()
        try:
            with self._semaphore:
                self.runner(record_id)
            with self._lock:
                self._completed[record_id] = time.time()
            self._log_event({
                "event": "completed",
                "record_id": record_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
            })
        except Exception as exc:  # pragma: no cover - surfaced through logs/health
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log_event({
                "event": "failed",
                "record_id": record_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
            })
        finally:
            with self._lock:
                self._active.discard(record_id)


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
        if self.path.rstrip("/") not in ("", "/trigger", "/webhook"):
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
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
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

        record_id = extract_record_id(payload)
        response = self.service.submit(record_id)
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
    print("健康检查: /health；生产环境请通过 HTTPS 反向代理暴露，并配置 FEISHU_AUTOMATION_TOKEN")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
