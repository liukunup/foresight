"""Prometheus metrics.

Label discipline: `channel_id` and `guild_id` are bounded and safe.
`user_id` must never be used as a label — the cardinality would melt
Prometheus.

The registry and the collectors of the collector live in
`foresight.metrics`: the collector must reach them without depending on
the API layer. They are re-exported here, so that this module stays the
one place an importer looks for a metric.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from foresight.metrics import (
    REGISTRY,
    media_bytes_total,
    media_downloads_total,
    messages_captured,
    messages_deleted,
    messages_edited,
    reactions_total,
    writer_batches_total,
    writer_batch_size,
    writer_queue_depth,
)

__all__ = [
    "REGISTRY",
    "ai_queue_depth",
    "ai_request_duration_seconds",
    "ai_requests_total",
    "ai_tokens_total",
    "api_request_duration_seconds",
    "api_requests_total",
    "backfill_messages_total",
    "db_attachments_total",
    "db_messages_total",
    "get_metrics",
    "media_bytes_total",
    "media_downloads_total",
    "messages_captured",
    "messages_deleted",
    "messages_edited",
    "metrics_content_type",
    "reactions_total",
    "writer_batches_total",
    "writer_batch_size",
    "writer_queue_depth",
]

backfill_messages_total = Counter(
    "foresight_backfill_messages_total",
    "Messages imported by backfill",
    ["channel_id"],
    registry=REGISTRY,
)
ai_queue_depth = Gauge(
    "foresight_ai_queue_depth",
    "Messages waiting for AI analysis",
    registry=REGISTRY,
)
ai_requests_total = Counter(
    "foresight_ai_requests_total",
    "LLM analysis calls",
    ["status"],
    registry=REGISTRY,
)
ai_request_duration_seconds = Histogram(
    "foresight_ai_request_duration_seconds",
    "LLM analysis latency",
    registry=REGISTRY,
)
ai_tokens_total = Counter(
    "foresight_ai_tokens_total",
    "LLM tokens consumed",
    ["type"],
    registry=REGISTRY,
)
api_requests_total = Counter(
    "foresight_api_requests_total",
    "API requests",
    ["method", "path", "status"],
    registry=REGISTRY,
)
api_request_duration_seconds = Histogram(
    "foresight_api_request_duration_seconds",
    "API request latency",
    ["method", "path"],
    registry=REGISTRY,
)
db_messages_total = Gauge(
    "foresight_db_messages_total",
    "Messages stored in the database",
    registry=REGISTRY,
)
db_attachments_total = Gauge(
    "foresight_db_attachments_total",
    "Attachments stored in the database",
    registry=REGISTRY,
)


def get_metrics() -> bytes:
    """Render the registry in Prometheus exposition format."""
    return generate_latest(REGISTRY)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
