# Prometheus 指标

`GET /metrics` 暴露全部指标，例如 `foresight_messages_captured_total`、`foresight_writer_queue_depth`、`foresight_writer_dropped_total`、`foresight_api_requests_total`、`foresight_db_messages_total`。

`foresight_writer_dropped_total` 按 `kind` 和 `reason`（`queue_full` / `stopped`）统计被写队列丢掉的写入操作 —— 队列满了或者写队列已经停了的时候，操作不会被写进数据库，这个计数让丢失看得见。

> 指标标签不会使用 `user_id`（基数太高会打爆 Prometheus），用户维度请查 API。
