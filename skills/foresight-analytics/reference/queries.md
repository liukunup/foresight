# Ready made queries

Timestamps are UTC ISO-8601 strings. SQLite, PostgreSQL and MySQL all sort
them correctly with plain string comparison.

The API filter `since`/`until` is the SQL bound `created_at >= '...'` /
`created_at <= '...'`. Use a full timestamp with the offset
(`2026-11-11T00:00:00+00:00`): a bare date compares before every message
of that day and would drop it from an `until` bound.

## Who talks the most

```sql
SELECT author_id, COUNT(*) AS message_count, SUM(char_count) AS total_chars
FROM messages
WHERE created_at >= '2026-11-01T00:00:00+00:00'
GROUP BY author_id
ORDER BY message_count DESC
LIMIT 20
```

## Daily volume

```sql
SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS message_count
FROM messages
GROUP BY day
ORDER BY day
```

`SUBSTR(created_at, 1, 10)` is the portable way to truncate a timestamp to a
day. Use `SUBSTR(created_at, 1, 13)` for the hour.

## Hourly activity

```sql
SELECT SUBSTR(created_at, 1, 13) AS hour, COUNT(*) AS message_count
FROM messages
GROUP BY hour
ORDER BY hour
```

## Most active channels

```sql
SELECT channel_id, COUNT(*) AS message_count
FROM messages
GROUP BY channel_id
ORDER BY message_count DESC
LIMIT 10
```

## Reply chains

```sql
SELECT m.message_id, m.reply_to_message_id, m.content, p.content AS parent
FROM messages m
LEFT JOIN messages p ON p.message_id = m.reply_to_message_id
WHERE m.reply_to_message_id IS NOT NULL
ORDER BY m.created_at DESC
LIMIT 50
```

## Deleted and edited messages

```sql
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted,
  SUM(CASE WHEN edited_at IS NOT NULL THEN 1 ELSE 0 END) AS edited
FROM messages
```

A deleted message keeps its `content`, so it still counts towards the
totals above. To see only the deleted ones, add
`WHERE deleted_at IS NOT NULL`.

## Negative sentiment with extracted issues

```sql
SELECT m.created_at, m.channel_id, m.content, a.issues_json, a.sentiment_score
FROM messages m
JOIN message_analysis a ON a.message_id = m.message_id
WHERE a.sentiment = 'negative'
ORDER BY a.sentiment_score DESC
LIMIT 50
```

## Most mentioned problems

```sql
SELECT message_id, issues_json
FROM message_analysis
WHERE issues_json IS NOT NULL AND issues_json <> '[]'
LIMIT 100
```

`issues_json` is a JSON array of strings. The endpoint
`GET /api/stats/topics` ranks them; this query gives the raw rows.

## Longest messages

```sql
SELECT message_id, author_id, char_count, word_count, content
FROM messages
ORDER BY char_count DESC
LIMIT 20
```

## Reaction leaderboard

```sql
SELECT emoji, COUNT(*) AS uses
FROM reactions
WHERE removed_at IS NULL
GROUP BY emoji
ORDER BY uses DESC
LIMIT 20
```

A removed reaction stays in the table, so the `removed_at IS NULL` filter
is what makes this an "active reactions" list.

## Attachments overview

```sql
SELECT storage_backend, status, COUNT(*) AS files, SUM(size) AS total_bytes
FROM attachments
GROUP BY storage_backend, status
```

## Messages that are still waiting for their analysis

```sql
SELECT COUNT(*) AS pending
FROM messages
WHERE analysis_status = 'pending'
```

A large number here means the AI worker has not caught up; `foresight
analyze` analyses what is left.

## One message and its analysis

```sql
SELECT m.message_id, m.content, a.sentiment, a.issues_json, a.translated_text
FROM messages m
LEFT JOIN message_analysis a ON a.message_id = m.message_id
WHERE m.message_id = 123456789012345678
```
