"""Prometheus metrics.

Label discipline: `channel_id` and `guild_id` are bounded and safe.
`user_id` must never be used as a label — the cardinality would melt
Prometheus.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

messages_captured = Counter(
    "foresight_messages_captured_total",
    "Messages captured from Discord",
    ["channel_id", "guild_id"],
    registry=REGISTRY,
)
messages_deleted = Counter(
    "foresight_messages_deleted_total",
    "Messages soft deleted",
    ["channel_id"],
    registry=REGISTRY,
)
messages_edited = Counter(
    "foresight_messages_edited_total",
    "Messages edited",
    ["channel_id"],
    registry=REGISTRY,
)
reactions_total = Counter(
    "foresight_reactions_total",
    "Reactions received",
    ["channel_id"],
    registry=REGISTRY,
)
backfill_messages_total = Counter(
    "foresight_backfill_messages_total",
    "Messages imported by backfill",
    ["channel_id"],
    registry=REGISTRY,
)
writer_queue_depth = Gauge(
    "foresight_writer_queue_depth",
    "Pending write operations",
    registry=REGISTRY,
)
writer_batches_total = Counter(
    "foresight_writer_batches_total",
    "Write batches flushed",
    ["status"],
    registry=REGISTRY,
)
writer_batch_size = Histogram(
    "foresight_writer_batch_size",
    "Rows per write batch",
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
media_downloads_total = Counter(
    "foresight_media_downloads_total",
    "Attachment downloads",
    ["status"],
    registry=REGISTRY,
)
media_bytes_total = Counter(
    "foresight_media_bytes_total",
    "Bytes downloaded",
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
