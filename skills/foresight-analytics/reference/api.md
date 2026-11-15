# API reference

Base URL comes from `~/.config/foresight/connection.toml` (`api_base_url`).
All requests need `Authorization: Bearer <token>` where the token is the
environment variable named by `token_env` (default `FORESIGHT_API_TOKEN`).

A service that is configured without a token accepts requests without the
header. It only binds to the loopback interface in that mode.

## Envelope

List endpoints return:

| Field | Meaning |
|---|---|
| `total` | the number of matching rows |
| `limit` | page size that was applied |
| `offset` | offset that was applied |
| `has_more` | `true` when more rows exist |
| `items` | the rows on this page |

`limit` default 50, maximum 500. A limit above the maximum is clamped
instead of rejected.

What `total` counts depends on the endpoint:

- `/api/messages` and `/api/reactions` count **rows**.
- `/api/users` and `/api/channels` count **groups**: `total` is the number
  of speakers or channels, and `items` holds one row per group, however
  many messages that group carries.
- `/api/stats/top-talkers`, `/api/stats/activity` and `/api/stats/topics`
  count the rows of the **response**. They carry an extra `truncated`
  flag, which is `true` when the list was cut at `limit` and there are
  therefore more rows than the ones returned. They are not paged: their
  `offset` is always `0` and `has_more` repeats `truncated`.

`GET /api/stats/overview` is not a list. It answers with a flat object.

## Time range

Every stats endpoint accepts `since` and `until` as ISO-8601 timestamps in
UTC, and both bounds are inclusive. A number without a time range covers
the whole archive, which is almost never what the user meant — always pick
a range and report it.

Use a **full timestamp with the offset**, e.g. `2026-11-11T00:00:00+00:00`.
A bare date such as `2026-11-11` works as well: it is the first second of
the day for `since` and the last one for `until`, so it names the whole
day. A value that is neither a timestamp nor a date is refused with a 422.

## Endpoints

### `GET /api/health`

`status`, `total_messages`, `total_attachments`, `db_size_mb` and
`last_captured_at` — the moment the newest row was stored, not the moment
it was sent.

### `GET /api/messages`

| Parameter | Type | Notes |
|---|---|---|
| `channel_id` | int | exact match |
| `author_id` | int | exact match |
| `guild_id` | int | exact match |
| `since` | string | ISO-8601, inclusive lower bound on `created_at` |
| `until` | string | ISO-8601, inclusive upper bound on `created_at` |
| `q` | string | case insensitive substring of `content` |
| `min_len` | int | minimum `char_count` |
| `has_attachment` | bool | `true` = has attachments, `false` = has none |
| `deleted` | bool | `true` = soft deleted, `false` = still alive |
| `edited` | bool | `true` = was edited, `false` = never edited |
| `order` | string | `asc` or `desc` by `created_at` (default `desc`) |
| `limit`, `offset` | int | pagination |

The bounds are compared as strings, so a full ISO-8601 timestamp such as
`2026-11-11T00:00:00+00:00` is used as it is. A bare date
(`2026-11-11`) is expanded to the first second of the day for `since` and
to the last one for `until`, so it names the whole day.

`q` matches a case insensitive substring. SQLite folds ASCII only, so a
search for a non-ASCII upper case letter does not match its lower case
form there; PostgreSQL and MySQL fold every script.

Each item contains the message fields listed in `schema.md`.

### `GET /api/messages/{message_id}`

One message together with its AI analysis under `analysis`, or `404`. The
`analysis` is `null` when the message was never analysed.

### `GET /api/users`

Speakers with `message_count`, `first_message_at`, `last_message_at`,
`avg_chars`. Supports `channel_id`, `limit`, `offset`.

A speaker is an author that wrote at least one message. `username` is
`null` when the author is not in the `users` table.

### `GET /api/users/{author_id}`

One speaker, or `404` when the author wrote no message.

### `GET /api/channels`

Channels with `message_count` and `last_message_at`. Supports `guild_id`,
`limit`, `offset`.

A channel is a channel that carries at least one message. `name` is `null`
when the channel is not in the `channels` table.

### `GET /api/reactions`

Reaction rows. Supports `message_id`, `only_active` (hide soft removed
reactions), `limit`, `offset`.

### `GET /api/stats/overview`

`total_messages`, `unique_authors`, `deleted_messages`, `edited_messages`,
`avg_chars`. Supports `since`, `until`, `channel_id`.

### `GET /api/stats/top-talkers`

`items` of `author_id`, `username`, `message_count`, `total_chars`.
Supports `since`, `until`, `channel_id`, `limit`. Carries `truncated`.

### `GET /api/stats/activity`

`items` of `bucket` and `message_count`. The `bucket` parameter is the
*mode* — `hour` or `day`, `day` by default — and the `bucket` field of an
item is the truncated timestamp of that period (`2026-11-01` for a day,
`2026-11-01T10` for an hour). Supports `since`, `until`, `channel_id`,
`limit`. Carries `truncated`.

### `GET /api/stats/topics`

`items` of `topic` and `count`. Aggregates the issues extracted by the AI
analysis; falls back to frequent words when no analysis is available.
Supports `since`, `until`, `channel_id`, `limit`. Carries `truncated`.

### `POST /api/query`

Free-form read-only SQL.

Request:

```json
{
  "sql": "SELECT ... WHERE author_id = :author",
  "params": {"author": 123},
  "limit": 100,
  "offset": 0
}
```

Response:

```json
{"columns": ["a"], "rows": [[1]], "row_count": 1, "elapsed_ms": 3}
```

`limit` defaults to 100 and is capped at 500. The named parameters are
written `:name` and are bound by the database, not pasted into the
statement.

Only a single `SELECT` or `WITH` statement is allowed. Writes are rejected
by the database engine itself, not by a text filter.

## Errors

| Status | Meaning |
|---|---|
| 401 | missing or wrong bearer token |
| 400 | the SQL was not a single read-only statement |
| 404 | the requested message or user does not exist |
| 422 | a query parameter had the wrong type or range |
| 500 | database error |
