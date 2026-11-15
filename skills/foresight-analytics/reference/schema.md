# Schema

All timestamps are UTC ISO-8601 strings (`2026-11-11T13:45:00+00:00`), so
string comparison sorts them chronologically. All JSON columns hold JSON
strings in a TEXT column.

## messages

| Column | Type | Meaning |
|---|---|---|
| `message_id` | bigint | Discord message id, primary key |
| `guild_id` | bigint | server id |
| `channel_id` | bigint | channel id |
| `author_id` | bigint | author id |
| `content` | text | message body — **the latest version**, edits do not keep history |
| `message_type` | int | Discord message type |
| `created_at` | text | when it was sent |
| `captured_at` | text | when ForeSight stored it |
| `edited_at` | text | last edit time, NULL when never edited |
| `edit_count` | int | how often it was edited |
| `deleted_at` | text | soft delete time, NULL when alive |
| `reply_to_message_id` | bigint | the message it replies to (id only) |
| `mentions_count` | int | number of @mentions |
| `char_count` | int | length of `content` in characters |
| `word_count` | int | words; CJK characters count one each |
| `attachment_count` | int | number of attachments |
| `attachments_json` | text | JSON array of attachment summaries |
| `embeds_json` | text | JSON array of embeds |
| `is_bot` | bool | whether the author is a bot |
| `analysis_status` | text | `pending` / `done` / `failed` / `skipped` |

Deleted messages keep their `content`: `deleted_at IS NOT NULL` means it was
deleted, and it still counts towards the totals.

`analysis_status` is `pending` until the AI worker has looked at the
message, `done` when the analysis succeeded, `failed` when it did not, and
`skipped` when the message was never a candidate — a message imported by
`foresight backfill` is skipped on purpose.

## users

`user_id`, `username`, `global_name`, `display_name`, `discriminator`,
`avatar_url`, `is_bot`, `account_created_at`, `guild_joined_at`,
`roles_json` (JSON array of role ids), `updated_at`.

## channels

`channel_id`, `guild_id`, `name`, `topic`, `channel_type`, `position`,
`captured_at`.

## guilds

`guild_id`, `name`, `member_count`, `captured_at`.

## reactions

`id`, `message_id`, `user_id`, `emoji`, `emoji_id`, `is_custom`, `added_at`,
`removed_at`.

A reaction is **soft removed**: it stays in the table with `removed_at` set.
Count active reactions with `removed_at IS NULL`.

## attachments

`id`, `message_id`, `attachment_id`, `filename`, `content_type`, `size`,
`url` (Discord CDN link), `sha256`, `storage_backend` (`local` / `s3`),
`storage_key`, `storage_uri`, `status` (`pending` / `stored` / `failed` /
`skipped`), `error`, `captured_at`, `stored_at`.

## message_analysis

One row per analysed message.

| Column | Meaning |
|---|---|
| `message_id` | primary key, joins to `messages` |
| `translated_text` | translation into the configured target language |
| `sentiment` | `positive` / `neutral` / `negative` / `mixed` |
| `sentiment_score` | confidence between 0 and 1 |
| `issues_json` | JSON array of problems, requests or feedback found |
| `model` | model that produced the result |
| `prompt_version` | prompt template version |
| `latency_ms` | call duration |
| `error` | failure reason when the analysis failed |
| `analyzed_at` | when the analysis finished |

The analysis of a message is **not** part of the `messages` row. The list
endpoint `/api/messages` does not carry it; `GET /api/messages/{id}` does,
under `analysis`. A message that was never analysed has no row here at
all.

`issues_json` holds a JSON array of strings — the issues the model found.
It is free text, so a query over it has to expect an empty array.

Join example:

```sql
SELECT m.created_at, m.content, a.sentiment, a.issues_json
FROM messages m
LEFT JOIN message_analysis a ON a.message_id = m.message_id
WHERE a.sentiment = 'negative'
ORDER BY m.created_at DESC
LIMIT 50
```
