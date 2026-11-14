"""The metric registry and the collectors of the collector.

The registry is process wide: the collector — the events and the writer —
and the API publish into the same one, so a single `/metrics` shows both
sides of the service.

The collectors live here and not in `foresight.api.metrics`, because the
collector must not depend on the API layer: the API reads the dump, the
collector writes it, and neither of them may reach into the code of the
other. This module depends on `prometheus_client` and on nothing else, so
the domain layer stays free of third party imports.

Label discipline: `channel_id` and `guild_id` are bounded and safe.
`user_id` must never be used as a label — the cardinality would melt
Prometheus.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

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
