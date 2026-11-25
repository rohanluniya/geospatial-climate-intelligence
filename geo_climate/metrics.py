"""Thread-safe Prometheus text exposition metrics registry for geospatial pipeline & tile API."""

from __future__ import annotations

import threading
from collections import defaultdict


class MetricsRegistry:
    """In-process Prometheus exposition metrics registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hist_sum: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._hist_count: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._help: dict[str, tuple[str, str]] = {
            "geoclimate_scenes_ingested_total": (
                "counter",
                "Total satellite scenes processed by status and region.",
            ),
            "geoclimate_quarantined_records_total": (
                "counter",
                "Total invalid or corrupt geospatial/weather records quarantined to DLQ.",
            ),
            "geoclimate_tile_requests_total": (
                "counter",
                "Total PNG map tile requests by layer and cache status.",
            ),
            "geoclimate_http_requests_total": (
                "counter",
                "Total HTTP API requests by path, method, and status code.",
            ),
            "geoclimate_auth_rejections_total": (
                "counter",
                "Total unauthorized or forbidden API access attempts.",
            ),
            "geoclimate_dq_checks_status": (
                "gauge",
                "Latest data quality check pass status (1=pass, 0=fail) by check name.",
            ),
            "geoclimate_pipeline_stage_duration_ms": (
                "summary",
                "Observed duration in milliseconds of geospatial pipeline stages.",
            ),
            "geoclimate_tile_render_duration_ms": (
                "summary",
                "Observed duration in milliseconds of map tile rendering/serving.",
            ),
            "geoclimate_active_iceberg_snapshots": (
                "gauge",
                "Number of active Iceberg V2 snapshots by table.",
            ),
        }

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._hist_sum[key] += value
            self._hist_count[key] += 1

    def snapshot_counters(self) -> dict[str, float]:
        with self._lock:
            out: dict[str, float] = {}
            for (name, labels), val in self._counters.items():
                lbl = ",".join(f"{k}={v}" for k, v in labels)
                out[f"{name}{{{lbl}}}" if lbl else name] = val
            return out

    def render_prometheus(self) -> str:
        lines: list[str] = []
        emitted_headers: set[str] = set()

        def ensure_header(metric_name: str) -> None:
            if metric_name not in emitted_headers:
                mtype, help_text = self._help.get(metric_name, ("gauge", metric_name))
                lines.append(f"# HELP {metric_name} {help_text}")
                lines.append(f"# TYPE {metric_name} {mtype}")
                emitted_headers.add(metric_name)

        def fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
            if not labels:
                return ""
            parts = [f'{k}="{v.replace(chr(34), chr(39))}"' for k, v in labels]
            return "{" + ",".join(parts) + "}"

        with self._lock:
            for (name, labels), val in sorted(self._counters.items()):
                ensure_header(name)
                lines.append(f"{name}{fmt_labels(labels)} {val}")
            for (name, labels), val in sorted(self._gauges.items()):
                ensure_header(name)
                lines.append(f"{name}{fmt_labels(labels)} {val}")
            for (name, labels), total in sorted(self._hist_sum.items()):
                ensure_header(name)
                cnt = self._hist_count[(name, labels)]
                lines.append(f"{name}_sum{fmt_labels(labels)} {round(total, 4)}")
                lines.append(f"{name}_count{fmt_labels(labels)} {cnt}")

        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()
