"""Structured JSON logging with trace/correlation/run IDs, secret redaction, and trace export."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_GH_FINE_TOKEN_PREFIX = "github_" + "pat_"
SENSITIVE_PATTERNS = (
    re.compile(rf"({_GH_FINE_TOKEN_PREFIX}[A-Za-z0-9_]+)"),
    re.compile(r"(Bearer\s+[A-Za-z0-9._\-]+)", re.IGNORECASE),
    re.compile(r"(dev-(?:viewer|analyst|admin)-token-[A-Za-z0-9_-]+)"),
    re.compile(r"(password[\"']?\s*[:=]\s*[\"']?[^\"',\s}]+)", re.IGNORECASE),
)


def redact_sensitive(text: str) -> str:
    """Redact bearer tokens, API keys, and passwords from log/audit strings."""
    cleaned = text
    for pattern in SENSITIVE_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter with trace/run correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive(record.getMessage()),
            "trace_id": getattr(record, "trace_id", None),
            "run_id": getattr(record, "run_id", None),
            "component": getattr(record, "component", "geo_climate"),
        }
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            for k, v in extra_fields.items():
                if isinstance(v, str):
                    payload[k] = redact_sensitive(v)
                else:
                    payload[k] = v
        return json.dumps(payload, default=str)


def get_logger(name: str = "geo_climate") -> logging.Logger:
    """Return a configured JSON logger without duplicating handlers."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextlib.contextmanager
def traced_span(
    span_name: str,
    trace_path: Path | None = None,
    trace_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Lightweight OpenTelemetry-compatible JSONL span emitter for local & CI observability."""
    tid = trace_id or new_trace_id()
    span_id = uuid.uuid4().hex[:16]
    start_ns = time.time_ns()
    span_record: dict[str, Any] = {
        "trace_id": tid,
        "span_id": span_id,
        "name": span_name,
        "start_time_utc": datetime.now(UTC).isoformat(),
        "status": "OK",
        "attributes": attributes or {},
    }
    try:
        yield span_record
    except Exception as exc:
        span_record["status"] = "ERROR"
        span_record["error"] = redact_sensitive(str(exc))
        raise
    finally:
        end_ns = time.time_ns()
        span_record["duration_ms"] = round((end_ns - start_ns) / 1_000_000.0, 3)
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(span_record, default=str) + "\n")


def record_security_audit(
    audit_path: Path,
    *,
    action: str,
    actor_role: str,
    endpoint: str,
    status_code: int,
    trace_id: str,
    detail: str = "",
) -> None:
    """Append an auditable security access event with secret redaction."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "trace_id": trace_id,
        "action": action,
        "actor_role": actor_role,
        "endpoint": endpoint,
        "status_code": status_code,
        "detail": redact_sensitive(detail),
    }
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
