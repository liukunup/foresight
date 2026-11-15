---
name: foresight-analytics
description: >
  Query and analyse a Discord chat archive stored by the ForeSight service.
  Use when the user asks about Discord chat statistics, who talks the most,
  message volume over time, sentiment or mood of conversations, problems or
  feature requests raised in chat, deleted or edited messages, or wants to
  export message rows. Also use when the user asks to analyse a channel dump
  or to dig through archived chat history.
---

# ForeSight Analytics

Query the ForeSight HTTP API and turn the result into a clear answer.

The API is **read only**. Everything below is a read: never try to insert,
update or delete anything.

## Before you start

1. Read `~/.config/foresight/connection.toml`. It contains `api_base_url`
   and `token_env`:

   ```bash
   cat ~/.config/foresight/connection.toml
   ```

   The file never holds the token itself — it only names the environment
   variable that holds the token.

2. Read the token from the environment variable named by `token_env`
   (`FORESIGHT_API_TOKEN` by default):

   ```bash
   echo "${FORESIGHT_API_TOKEN:-NOT_SET}"
   ```

   If it prints `NOT_SET`, tell the user to export it and stop. The
   commands below use the default name; use the one the connection file
   names when it is different.

3. Check that the API is reachable:

   ```bash
   api_base_url="http://127.0.0.1:8000"   # the api_base_url of the file

   curl -s -H "Authorization: Bearer $FORESIGHT_API_TOKEN" \
        "$api_base_url/api/health"
   ```

   If the connection fails, tell the user to start the service with
   `foresight serve` (or `docker compose up`) and stop.

## Calling the API

Every request needs the bearer token.

```bash
api_base_url="http://127.0.0.1:8000"

curl -s -H "Authorization: Bearer $FORESIGHT_API_TOKEN" \
     "$api_base_url/api/stats/overview"
```

### Two shapes of answer

`GET /api/stats/overview` is a **flat object** — `total_messages`,
`unique_authors`, `deleted_messages`, `edited_messages`, `avg_chars` — and
not a list.

Every **list** endpoint returns the same envelope:

```json
{"total": 1200, "limit": 50, "offset": 0, "has_more": true, "items": []}
```

`limit` defaults to 50 and is capped at 500. When `has_more` is `true`,
fetch the next page by increasing `offset` by `limit` until it is `false`.
Never assume you saw everything from the first page.

Two exceptions change what `total` means, so read them before quoting a
number:

- `/api/users` and `/api/channels` count **groups**: `total` is the number
  of speakers or channels, and `items` holds one row per group. It is
  never the number of messages.
- The three analytics endpoints (`/api/stats/top-talkers`,
  `/api/stats/activity`, `/api/stats/topics`) carry an extra `truncated`
  flag. It is `true` when the result was cut at `limit`, which means there
  are more rows than the ones you received. They cannot be paged: their
  `offset` is always `0` and their `total` counts the rows of the
  response, not of the whole dump.

### Always state the time range

Every stats endpoint accepts `since` and `until` as ISO-8601 timestamps in
UTC. A number without a stated time range is almost never what the user
meant: pick the range out of the question ("this week", "since the
launch", "in July") and say it in the answer.

Use a **full timestamp with the offset**, e.g. `2026-11-11T00:00:00+00:00`.
A bare date such as `2026-11-11` works as well: it is the first second of
the day for `since` and the last one for `until`, so it names the whole
day. A value that is neither a timestamp nor a date is refused with a 422.

### Free-form SQL

For free-form analysis use the read-only SQL endpoint:

```bash
curl -s -X POST -H "Authorization: Bearer $FORESIGHT_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"sql":"SELECT channel_id, COUNT(*) c FROM messages GROUP BY 1 ORDER BY c DESC","params":{},"limit":20}' \
     "$api_base_url/api/query"
```

The helper that ships with this skill does the same thing and prints a
table. It sits next to this file, in `scripts/query.py`, and its path is
in the connection file — so the command works from any directory you
work in:

```bash
helper="$(sed -n 's/^helper_path = "\(.*\)"$/\1/p' ~/.config/foresight/connection.toml)"
python3 "$helper" \
    --sql "SELECT channel_id, COUNT(*) c FROM messages GROUP BY 1"
```

The helper reads the same connection file and the same environment
variable. It never writes the token to disk.

## Common questions

| The user asks | Call |
|---|---|
| How much chat is there? | `GET /api/stats/overview` |
| Who talks the most? | `GET /api/stats/top-talkers` |
| How many people spoke? | `GET /api/users` |
| Which channels are busy? | `GET /api/channels` |
| When are people active? | `GET /api/stats/activity?bucket=hour` |
| What are people complaining about? | `GET /api/stats/topics` for the ranking, then `POST /api/query` over `message_analysis` for the messages behind it |
| Is the mood negative? | `POST /api/query` over `message_analysis` (`sentiment`), or `GET /api/messages/{id}` for a single message |
| What did someone say? | `GET /api/messages?author_id=<id>` |
| What did one message get analysed as? | `GET /api/messages/{id}` — the answer carries `analysis`, which is `null` when the message was never analysed |
| Were messages deleted? | `GET /api/messages?deleted=true` |
| Show me one channel | `GET /api/messages?channel_id=<id>` |
| Something custom | `POST /api/query` |

## Reporting back

- Lead with the **conclusion and the numbers**, then the supporting detail.
  "User A drove 38% of this week's messages (124 of 320)."
- Do not dump raw SQL result rows at the user. Summarise, and quote at most
  a handful of representative messages.
- Always state the **time range** the numbers cover.
- If a page was truncated (`truncated: true`), say how many rows you
  actually looked at.
- If the API returns an empty list, say so plainly instead of speculating.

## What the API cannot do

Two jobs belong to the operator, not to the agent, because they write to
the dump or call the model:

- **Import history** that the collector never saw: `foresight backfill
  --days 30`.
- **Analyse what is still pending**: `foresight analyze`.

Suggest them to the user when the archive is too small, too old or too
unanalysed for the question.

## Boundaries

- The API is **read only**. Never try to insert, update or delete anything.
- `POST /api/query` accepts a single `SELECT`/`WITH` statement only. Writes
  are rejected by the database engine.
- The SQL dialect follows the configured database: SQLite uses `strftime`,
  PostgreSQL uses `to_char`, MySQL uses `DATE_FORMAT`. Timestamps are stored
  as UTC ISO-8601 strings, so `SUBSTR(created_at, 1, 10)` truncates a
  timestamp to a day on every backend — it is the portable way, and it is
  what the service itself uses.
- Timestamps are UTC. Convert to the user's timezone only if they ask.

## Reference

- `reference/api.md` — every endpoint, parameter and response field
- `reference/schema.md` — table structure and the exact meaning of each column
- `reference/queries.md` — ready made analysis queries
