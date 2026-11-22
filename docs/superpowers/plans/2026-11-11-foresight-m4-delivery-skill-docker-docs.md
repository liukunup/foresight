# ForeSight M4 — Skill、Docker 与文档 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付「AI 可用的查询入口」与「开箱即用的部署方式」：一个可安装的 Agent Skill、一个安装脚本、一个 Docker 镜像 + docker-compose、以及一份能让新用户跑起来的 README。

**Architecture:** Skill 是纯文本资产（SKILL.md + 参考文档 + 一个查询脚本），通过 `scripts/install_skill.sh` 复制到目标 agent 的 skills 目录，并把 API 地址与连接信息写入 `~/.config/foresight/connection.toml`。Docker 用多阶段构建产出单一镜像，compose 用 `both` 入口在单进程内同时跑采集器与 API。

**Tech Stack:** Bash、Docker、Compose、Markdown、Python（skill 自带查询脚本）。

> 本文是当时的实施记录，步骤里的镜像版本和 Dockerfile 片段可能已随依赖升级变化；当前交付的部署方式以 `README.md` 和设计规范 §15 为准（单份 `docker-compose.yml`，用 `COMPOSE_PROFILES` 选择可选服务）。

**前置条件：** M1、M2、M3 已完成。

## Global Constraints

- M1/M2/M3 的全部 Global Constraints 继续生效
- Skill 目录名固定为 `foresight-analytics`，`SKILL.md` 必须带 `name` 与 `description` frontmatter
- Skill **只读**：任何情况下不得写入或修改数据库
- Skill 的连接信息来自 `~/.config/foresight/connection.toml`，token 只存**环境变量名**，不存明文
- 镜像基础随 Dependabot 升级（当前 Dockerfile 为 `python:3.15-slim`），运行时使用非 root 用户（uid 10001）
- compose 默认端口 **8000**，可通过 `.env` 覆盖
- compose 的 API 监听 `0.0.0.0`，因此 `.env` 必须包含 `FORESIGHT_API_TOKEN`，否则容器拒绝启动
- `./data` 卷同时承载数据库文件与本地媒体
- README 必须显著标注「需在 Discord 开发者后台开启 MESSAGE CONTENT INTENT」

---

## Task 19: Agent Skill 资产

**Files:**
- Create: `skills/foresight-analytics/SKILL.md`
- Create: `skills/foresight-analytics/reference/api.md`
- Create: `skills/foresight-analytics/reference/schema.md`
- Create: `skills/foresight-analytics/reference/queries.md`
- Create: `skills/foresight-analytics/scripts/query.py`

**Interfaces:**
- Consumes: M2 的 HTTP API 契约（端点、参数、分页信封、认证方式）
- Produces: 可安装的 skill 目录 `skills/foresight-analytics/`

- [ ] **Step 1: 写 SKILL.md**

创建 `skills/foresight-analytics/SKILL.md`：

```markdown
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

## Before you start

1. Read `~/.config/foresight/connection.toml`. It contains `api_base_url`,
   `token_env` and `db_path`.

   ```bash
   cat ~/.config/foresight/connection.toml
   ```

2. Read the token from the environment variable named by `token_env`:

   ```bash
   echo "${FORESIGHT_API_TOKEN:-NOT_SET}"
   ```

   If it prints `NOT_SET`, tell the user to export it and stop.

3. Check that the API is reachable:

   ```bash
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

Every list endpoint returns the same envelope:

```json
{"total": 1200, "limit": 50, "offset": 0, "has_more": true, "items": []}
```

`limit` defaults to 50 and is capped at 500. When `has_more` is `true`, fetch
the next page by increasing `offset` by `limit` until it is `false`. Never
assume you saw everything from the first page.

For free-form analysis use the read-only SQL endpoint:

```bash
curl -s -X POST -H "Authorization: Bearer $FORESIGHT_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"sql":"SELECT channel_id, COUNT(*) c FROM messages GROUP BY 1 ORDER BY c DESC","params":{},"limit":20}' \
     "$api_base_url/api/query"
```

The bundled helper does the same thing and prints a table:

```bash
python3 skills/foresight-analytics/scripts/query.py \
    --sql "SELECT channel_id, COUNT(*) c FROM messages GROUP BY 1"
```

## Common questions

| The user asks | Call |
|---|---|
| How much chat is there? | `GET /api/stats/overview` |
| Who talks the most? | `GET /api/stats/top-talkers` |
| When are people active? | `GET /api/stats/activity?bucket=hour` |
| What are people complaining about? | `GET /api/messages` filtered, then read `analysis.issues`; or `GET /api/stats/topics` |
| Is the mood negative? | `GET /api/messages?q=...` plus the `sentiment` field, or SQL over `message_analysis` |
| What did someone say? | `GET /api/messages?author_id=<id>` |
| Were messages deleted? | `GET /api/messages?deleted=true` |
| Show me one channel | `GET /api/messages?channel_id=<id>` |
| Something custom | `POST /api/query` |

## Reporting back

- Lead with the **conclusion and the numbers**, then the supporting detail.
  "User A drove 38% of this week's messages (124 of 320)."
- Do not dump raw SQL result rows at the user. Summarise, and quote at most
  a handful of representative messages.
- Always state the **time range** the numbers cover. Every stats endpoint
  accepts `since` and `until` as ISO-8601 timestamps; without them the
  numbers cover the whole archive, which is rarely what the user meant.
- If a page was truncated, say how many rows you actually looked at.
- If the API returns an empty list, say so plainly instead of speculating.

## Boundaries

- The API is **read only**. Never try to insert, update or delete anything.
- `POST /api/query` accepts a single `SELECT`/`WITH` statement only. Writes
  are rejected by the database engine.
- The SQL dialect follows the configured database: SQLite uses `strftime`,
  PostgreSQL uses `to_char`, MySQL uses `DATE_FORMAT`. Timestamps are stored
  as UTC ISO-8601 strings, so plain string comparison sorts them correctly.
- Timestamps are UTC. Convert to the user's timezone only if they ask.

## Reference

- `reference/api.md` — every endpoint, parameter and response field
- `reference/schema.md` — table structure and the exact meaning of each column
- `reference/queries.md` — ready made analysis queries
```

- [ ] **Step 2: 写 reference/api.md**

创建 `skills/foresight-analytics/reference/api.md`：

```markdown
# API reference

Base URL comes from `~/.config/foresight/connection.toml` (`api_base_url`).
All requests need `Authorization: Bearer <token>` where the token is the
environment variable named by `token_env` (default `FORESIGHT_API_TOKEN`).

## Envelope

List endpoints return:

| Field | Meaning |
|---|---|
| `total` | total number of matching rows |
| `limit` | page size that was applied |
| `offset` | offset that was applied |
| `has_more` | `true` when more rows exist |
| `items` | the rows on this page |

`limit` default 50, maximum 500.

## Endpoints

### `GET /api/health`
Status, total message count, total attachment count, database size in MB and
the timestamp of the most recent captured message.

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
| `has_attachment` | bool | `true` = has attachments |
| `deleted` | bool | `true` = soft deleted |
| `edited` | bool | `true` = was edited |
| `order` | string | `asc` or `desc` by `created_at` (default `desc`) |
| `limit`, `offset` | int | pagination |

Each item contains the message fields listed in `schema.md`.

### `GET /api/messages/{message_id}`
One message, or `404`.

### `GET /api/users`
Speakers with `message_count`, `first_message_at`, `last_message_at`,
`avg_chars`. Supports `channel_id`, `limit`, `offset`.

### `GET /api/users/{author_id}`
One speaker, or `404`.

### `GET /api/channels`
Channels with `message_count` and `last_message_at`. Supports `guild_id`,
`limit`, `offset`.

### `GET /api/reactions`
Reaction rows. Supports `message_id`, `only_active` (hide soft removed
reactions), `limit`, `offset`.

### `GET /api/stats/overview`
`total_messages`, `unique_authors`, `deleted_messages`, `edited_messages`,
`avg_chars`. Supports `since`, `until`, `channel_id`.

### `GET /api/stats/top-talkers`
`items` of `author_id`, `username`, `message_count`, `total_chars`.
Supports `since`, `until`, `channel_id`, `limit`.

### `GET /api/stats/activity`
`items` of `bucket` and `message_count`. `bucket` is `hour` or `day`.
Supports `since`, `until`, `channel_id`.

### `GET /api/stats/topics`
`items` of `topic` and `count`. Aggregates the issues extracted by the AI
analysis; falls back to frequent words when no analysis is available.
Supports `since`, `until`, `channel_id`, `limit`.

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

Only a single `SELECT` or `WITH` statement is allowed. Writes are rejected by
the database engine itself, not by a text filter.

## Errors

| Status | Meaning |
|---|---|
| 401 | missing or wrong bearer token |
| 400 | the SQL was not a single read-only statement |
| 404 | the requested message or user does not exist |
| 422 | a query parameter had the wrong type or range |
| 500 | database error |
```

- [ ] **Step 3: 写 reference/schema.md**

创建 `skills/foresight-analytics/reference/schema.md`：

```markdown
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

Join example:

```sql
SELECT m.created_at, m.content, a.sentiment, a.issues_json
FROM messages m
LEFT JOIN message_analysis a ON a.message_id = m.message_id
WHERE a.sentiment = 'negative'
ORDER BY m.created_at DESC
LIMIT 50
```
```

- [ ] **Step 4: 写 reference/queries.md**

创建 `skills/foresight-analytics/reference/queries.md`：

```markdown
# Ready made queries

Timestamps are UTC ISO-8601 strings. SQLite, PostgreSQL and MySQL all sort
them correctly with plain string comparison.

## Who talks the most

```sql
SELECT author_id, COUNT(*) AS messages, SUM(char_count) AS chars
FROM messages
WHERE created_at >= '2026-11-01T00:00:00+00:00'
GROUP BY author_id
ORDER BY messages DESC
LIMIT 20
```

## Daily volume

```sql
SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS messages
FROM messages
GROUP BY day
ORDER BY day
```

`SUBSTR(created_at, 1, 10)` is the portable way to truncate a timestamp to a
day. Use `SUBSTR(created_at, 1, 13)` for the hour.

## Hourly activity

```sql
SELECT SUBSTR(created_at, 1, 13) AS hour, COUNT(*) AS messages
FROM messages
GROUP BY hour
ORDER BY hour
```

## Most active channels

```sql
SELECT channel_id, COUNT(*) AS messages
FROM messages
GROUP BY channel_id
ORDER BY messages DESC
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

## Attachments overview

```sql
SELECT storage_backend, status, COUNT(*) AS files, SUM(size) AS bytes
FROM attachments
GROUP BY storage_backend, status
```
```

- [ ] **Step 5: 写 scripts/query.py**

创建 `skills/foresight-analytics/scripts/query.py`：

```python
#!/usr/bin/env python3
"""Run a read-only SQL statement against the ForeSight API.

Usage:
    python3 query.py --sql "SELECT COUNT(*) FROM messages"
    python3 query.py --sql "SELECT * FROM messages LIMIT 5" --format json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONNECTION_FILE = Path.home() / ".config" / "foresight" / "connection.toml"


def load_connection(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"connection file not found: {path}. Run scripts/install_skill.sh first."
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


def request_json(url: str, token: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API returned {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach the API: {exc.reason}") from exc


def print_table(columns: list[str], rows: list[list]) -> None:
    if not columns:
        return
    widths = [len(column) for column in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    header = " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    print(header)
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", required=True, help="Read-only SQL statement")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument(
        "--connection",
        type=Path,
        default=DEFAULT_CONNECTION_FILE,
        help="Path to connection.toml",
    )
    args = parser.parse_args()

    connection = load_connection(args.connection)
    base_url = str(connection.get("api_base_url", "http://127.0.0.1:8000")).rstrip("/")
    token_env = str(connection.get("token_env", "FORESIGHT_API_TOKEN"))
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise SystemExit(f"{token_env} is not set")

    body = request_json(
        f"{base_url}/api/query",
        token,
        {"sql": args.sql, "params": {}, "limit": args.limit, "offset": args.offset},
    )
    if args.format == "json":
        print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print_table(body.get("columns", []), body.get("rows", []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 提交**

```bash
git add skills/foresight-analytics
git commit -m "docs: add foresight-analytics agent skill"
```

---

## Task 20: Skill 安装脚本

**Files:**
- Create: `scripts/install_skill.sh`
- Create: `tests/unit/test_install_skill.py`

**Interfaces:**
- Consumes: `skills/foresight-analytics/`（Task 19）
- Produces: `scripts/install_skill.sh`，安装到 `~/.config/opencode/skills/` 或 `~/.claude/skills/`，并写入 `~/.config/foresight/connection.toml`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_install_skill.py`：

```python
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_skill.sh"


def test_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_script_copies_skill_and_writes_connection(tmp_path: Path):
    target = tmp_path / "skills"
    connection = tmp_path / "config" / "foresight" / "connection.toml"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--target-dir",
            str(target),
            "--connection-file",
            str(connection),
            "--api-base-url",
            "http://127.0.0.1:8000",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (target / "foresight-analytics" / "SKILL.md").exists()
    assert (target / "foresight-analytics" / "reference" / "schema.md").exists()
    assert connection.exists()
    content = connection.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000" in content
    assert "FORESIGHT_API_TOKEN" in content
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_install_skill.py -v`
Expected: FAIL —— 脚本不存在

- [ ] **Step 3: 实现安装脚本**

创建 `scripts/install_skill.sh`：

```bash
#!/usr/bin/env bash
# Install the foresight-analytics skill into an agent's skills directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKILL_NAME="foresight-analytics"
SOURCE_DIR="${REPO_ROOT}/skills/${SKILL_NAME}"

TARGET_DIR="${HOME}/.config/opencode/skills"
CONNECTION_FILE="${HOME}/.config/foresight/connection.toml"
API_BASE_URL="http://127.0.0.1:8000"
TOKEN_ENV="FORESIGHT_API_TOKEN"
SYMLINK=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

  --target-dir DIR       where the skill is installed
                         (default: ~/.config/opencode/skills)
  --connection-file FILE where connection.toml is written
                         (default: ~/.config/foresight/connection.toml)
  --api-base-url URL     base URL of the ForeSight API
                         (default: ${API_BASE_URL})
  --token-env NAME       environment variable holding the API token
                         (default: ${TOKEN_ENV})
  --target AGENT         shorthand: opencode | claude
  --symlink              symlink instead of copying (for development)
  -h, --help             show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-dir) TARGET_DIR="$2"; shift 2 ;;
        --connection-file) CONNECTION_FILE="$2"; shift 2 ;;
        --api-base-url) API_BASE_URL="$2"; shift 2 ;;
        --token-env) TOKEN_ENV="$2"; shift 2 ;;
        --target)
            case "$2" in
                opencode) TARGET_DIR="${HOME}/.config/opencode/skills" ;;
                claude) TARGET_DIR="${HOME}/.claude/skills" ;;
                *) echo "unknown target: $2" >&2; exit 2 ;;
            esac
            shift 2
            ;;
        --symlink) SYMLINK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [[ ! -d "${SOURCE_DIR}" ]]; then
    echo "skill source not found: ${SOURCE_DIR}" >&2
    exit 1
fi

mkdir -p "${TARGET_DIR}"
DEST="${TARGET_DIR}/${SKILL_NAME}"

if [[ "${SYMLINK}" -eq 1 ]]; then
    rm -rf "${DEST}"
    ln -s "${SOURCE_DIR}" "${DEST}"
else
    rm -rf "${DEST}"
    cp -R "${SOURCE_DIR}" "${DEST}"
fi

mkdir -p "$(dirname "${CONNECTION_FILE}")"
cat > "${CONNECTION_FILE}" <<EOF
# Written by install_skill.sh
api_base_url = "${API_BASE_URL}"
token_env = "${TOKEN_ENV}"
EOF

cat <<EOF
Installed ${SKILL_NAME} to ${DEST}
Wrote connection info to ${CONNECTION_FILE}

Next steps:
  1. export ${TOKEN_ENV}=<your api token>
  2. start the service:  foresight serve   (or: docker compose up)
  3. ask your agent to analyse the chat archive
EOF
```

赋予可执行权限：

```bash
chmod +x scripts/install_skill.sh
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_install_skill.py -v`
Expected: PASS —— 2 passed

- [ ] **Step 5: 提交**

```bash
git add scripts/install_skill.sh tests/unit/test_install_skill.py
git commit -m "feat: skill installation script"
```

---

## Task 21: Docker 镜像与 compose

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `tests/unit/test_docker_assets.py`

**Interfaces:**
- Consumes: M1/M2/M3 的 CLI 入口（`foresight run` / `serve` / `both`）
- Produces: `foresight:latest` 镜像；`docker compose up` 即可运行

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_docker_assets.py`：

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_exists_and_uses_non_root_user():
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.13-slim" in content
    assert "USER foresight" in content
    assert 'ENTRYPOINT ["foresight"]' in content


def test_compose_exposes_port_8000_and_persists_data():
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"8000:8000"' in content
    assert "./data:/app/data" in content
    assert 'command: ["both"]' in content


def test_env_example_lists_required_secrets():
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=" in content
    assert "FORESIGHT_API_TOKEN=" in content
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_docker_assets.py -v`
Expected: FAIL —— 文件不存在

- [ ] **Step 3: 写 Dockerfile**

创建 `Dockerfile`：

```dockerfile
# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --python /opt/venv/bin/python /build


FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    FORESIGHT_CONFIG=/app/config.toml

RUN groupadd --gid 10001 foresight \
    && useradd --uid 10001 --gid 10001 --create-home foresight \
    && mkdir -p /app/data \
    && chown -R foresight:foresight /app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=foresight:foresight src ./src
COPY --chown=foresight:foresight config.example.toml ./config.example.toml

USER foresight

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["foresight"]
```

- [ ] **Step 4: 写 compose、.dockerignore 与 .env.example**

创建 `docker-compose.yml`：

```yaml
services:
  foresight:
    build: .
    image: foresight:latest
    container_name: foresight
    command: ["both"]
    ports:
      - "8000:8000"
    env_file:
      - .env
    environment:
      FORESIGHT_API_HOST: "0.0.0.0"
      FORESIGHT_API_PORT: "8000"
      FORESIGHT_CONFIG: "/app/config.toml"
    volumes:
      - ./config.toml:/app/config.toml:ro
      - ./data:/app/data
    restart: unless-stopped
```

创建 `.dockerignore`：

```text
.git
.github
.venv
venv
__pycache__
*.pyc
*.egg-info
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
htmlcov
data
docs
skills
tests
config.toml
.env
```

创建 `.env.example`：

```text
# Copy to .env and fill in. Never commit .env.
DISCORD_TOKEN=
FORESIGHT_API_TOKEN=
FORESIGHT_LLM_API_KEY=
FORESIGHT_DB_PASSWORD=
FORESIGHT_S3_ACCESS_KEY_ID=
FORESIGHT_S3_SECRET_ACCESS_KEY=
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_docker_assets.py -v`
Expected: PASS —— 3 passed

- [ ] **Step 6: 构建镜像验证**

Run: `docker build -t foresight:latest .`
Expected: 构建成功

Run: `docker compose config`
Expected: 打印出解析后的配置，不报错

- [ ] **Step 7: 提交**

```bash
git add Dockerfile .dockerignore docker-compose.yml .env.example tests/unit/test_docker_assets.py
git commit -m "build: docker image and compose stack"
```

---

## Task 22: README 使用文档

**Files:**
- Create: `README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: 全部命令与配置项
- Produces: 新用户从零到跑起来的完整说明

- [ ] **Step 1: 写 README**

用 `write` 工具整体替换 `README.md`（当前内容只有 `# ForeSight` 一行）：

````markdown
# ForeSight

Discord 频道消息转储服务：只要用户在受监控的频道发言，消息就会连同编辑、删除、表情回应和附件一起落库，并通过 HTTP API、Prometheus 指标和一个可安装的 Agent Skill 供人和 AI 查询分析。

## 特性

- **实时采集**：消息、编辑、删除、表情回应全部捕获（编辑只保留最新内容，删除是软删除）
- **附件归档**：图片/视频/音频下载到本地或任意 S3 兼容存储（MinIO、R2、OSS）
- **AI 分析**：一次模型调用同时产出翻译、情绪、问题提取
- **查询 API**：资源端点 + 统计端点 + 只读 SQL 端点，全部只读连接
- **Agent Skill**：装上之后 AI 就能自己查、自己分析、自己出结论
- **Prometheus 指标**：采集、写入队列、AI、媒体、API 全链路可观测
- **三种数据库**：SQLite（默认）/ PostgreSQL / MySQL
- **历史补录**：一条命令把过去 N 天的消息导入

## 快速开始（Docker，推荐）

```bash
git clone https://github.com/liukunup/foresight.git
cd foresight

cp config.example.toml config.toml   # 填入 [discord].channel_ids
cp .env.example .env                 # 填入 DISCORD_TOKEN 与 FORESIGHT_API_TOKEN

docker compose up -d
```

服务会在 <http://127.0.0.1:8000> 提供 API，接口文档在 <http://127.0.0.1:8000/docs>。

## 快速开始（本地）

```bash
uv venv
uv sync

cp config.example.toml config.toml
export DISCORD_TOKEN="你的 bot token"
export FORESIGHT_API_TOKEN="随便生成一个随机串"

uv run foresight init-db
uv run foresight both        # 采集器 + API 同时跑
```

## 准备 Discord 机器人

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications)，新建 Application
2. 在 **Bot** 页签点 **Reset Token**，复制这个 token —— 它就是 `DISCORD_TOKEN`
3. 打开 **Privileged Gateway Intents**，**必须开启 MESSAGE CONTENT INTENT**（不开的话正文永远是空的）
4. 在 **OAuth2 → URL Generator** 勾选 `bot` + `Read Message History` + `Send Messages`，用生成的链接邀请 bot 进服务器
5. 在 Discord 客户端右键目标频道 → 复制频道 ID（开发者模式：设置 → 高级 → 开发者模式），填进 `config.toml` 的 `[discord].channel_ids`

> 频道 ID 也可以写进 `config.toml`，但**所有密钥必须用环境变量**：`DISCORD_TOKEN`、`FORESIGHT_API_TOKEN`、`FORESIGHT_LLM_API_KEY`、`FORESIGHT_DB_PASSWORD`、`FORESIGHT_S3_ACCESS_KEY_ID`、`FORESIGHT_S3_SECRET_ACCESS_KEY`。

## 配置

`config.toml` 完整示例见 [`config.example.toml`](config.example.toml)。

| 段 | 关键项 | 说明 |
|---|---|---|
| `[discord]` | `channel_ids` | 白名单频道 ID 列表，非空 |
| `[database]` | `backend` | `sqlite` / `postgresql` / `mysql` |
| `[media]` | `backend` | `local`（默认）/ `s3` |
| `[api]` | `host` / `port` | 默认 `127.0.0.1:8000` |
| `[ai]` | `enabled` / `base_url` / `model` | OpenAI 兼容接口 |
| `[writer]` | `batch_size` / `flush_interval_ms` | 批量落库参数 |

环境变量 `FORESIGHT_API_HOST`、`FORESIGHT_API_PORT`、`FORESIGHT_CONFIG` 的优先级高于配置文件。

## 命令

| 命令 | 作用 |
|---|---|
| `foresight run` | 只跑采集器 |
| `foresight serve` | 只跑 HTTP API |
| `foresight both` | 单进程同时跑采集器和 API（compose 默认） |
| `foresight init-db` | 建表 |
| `foresight backfill --days 7` | 导入最近 7 天的历史消息（不补表情、不跑 AI） |
| `foresight query "SELECT ..." --db data/foresight.db` | 只读查询 |
| `foresight stats --db data/foresight.db` | 概览统计 |

## 安装 AI Skill

```bash
./scripts/install_skill.sh --target opencode     # 或 --target claude
export FORESIGHT_API_TOKEN="..."                 # 脚本会提示
```

安装后 AI 就能直接问「上周谁发言最多」「大家都在抱怨什么」，它会自己查 API 并给出结论。连接信息写在 `~/.config/foresight/connection.toml`，token 只存环境变量名。

## API

所有请求需要 `Authorization: Bearer <FORESIGHT_API_TOKEN>`。

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 状态与计数 |
| `GET /api/messages` | 消息列表，支持频道/作者/时间/关键词过滤 |
| `GET /api/messages/{id}` | 单条消息 |
| `GET /api/users` | 发言排行 |
| `GET /api/channels` | 频道统计 |
| `GET /api/reactions` | 表情明细 |
| `GET /api/stats/overview` | 总量、活跃人数、删除率、编辑率 |
| `GET /api/stats/top-talkers` | 发言排行 |
| `GET /api/stats/activity` | 时间分布（`bucket=hour\|day`） |
| `GET /api/stats/topics` | 高频议题 |
| `POST /api/query` | 只读 SQL（单条 SELECT/WITH，写操作被数据库引擎拒绝） |

分页信封：`{"total": N, "limit": 50, "offset": 0, "has_more": true, "items": []}`。

## Prometheus 指标

`GET /metrics` 暴露全部指标，例如 `foresight_messages_captured_total`、`foresight_writer_queue_depth`、`foresight_api_requests_total`、`foresight_db_messages_total`。

> 指标标签不会使用 `user_id`（基数太高会打爆 Prometheus），用户维度请查 API。

## 常见问题

**正文是空的 / 抓不到消息**
Discord 开发者后台的 **MESSAGE CONTENT INTENT** 没开。在 Bot 页面打开后重启 bot。

**`SQLite is locked`**
服务在写库时又有人查。SQLite 已启用 WAL 和 5 秒 busy timeout；长时间锁通常是有别的进程在写同一个文件，确认没有多个容器共享同一个库文件。

**`FORESIGHT_API_TOKEN` 必须设置**
API 绑定到 `0.0.0.0`（非本机）时必须设置 token，否则服务拒绝启动。

**机器人报 403 / 看不到频道**
bot 没被邀请进服务器，或没有该频道的读取权限（`Read Message History`）。

**LLM 分析一直失败**
检查 `FORESIGHT_LLM_API_KEY` 和 `base_url` 是否指向 OpenAI 兼容接口。失败的行会在 `message_analysis.error` 里留下原因，`messages.analysis_status` 标记为 `failed`。

**下载附件 403**
Discord CDN 链接带签名且会过期，收到消息时应立即下载。如果附件一直是 `failed`，用 `foresight backfill` 重新导入。

## 开发

```bash
uv run pytest -v
uv run foresight init-db --config config.example.toml
```

## 许可

见 [LICENSE](LICENSE)。
````

- [ ] **Step 2: 提交**

```bash
git add README.md
git commit -m "docs: write the usage guide"
```

---

## Task 23: 端到端冒烟测试

**Files:**
- Create: `tests/integration/test_end_to_end.py`

**Interfaces:**
- Consumes: M1（采集）、M2（API）、M3（分析与媒体）
- Produces: 一条覆盖「消息进入 → 落库 → API 查到 → AI 分析写入」的测试

- [ ] **Step 1: 写测试**

创建 `tests/integration/test_end_to_end.py`：

```python
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.analysis.llm import LlmClient
from foresight.analysis.worker import AnalysisWorker
from foresight.api.app import create_app
from foresight.config import AiConfig
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.events import EventHandler
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import message_analysis, messages
from foresight.writer import Writer


class RecordingWriter:
    def __init__(self):
        self.ops = []

    def submit(self, op):
        self.ops.append(op)
        return True


def _fake_message(content: str):
    return SimpleNamespace(
        id=100,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=SimpleNamespace(
            id=30,
            name="alice",
            global_name="Alice",
            display_name="alice",
            discriminator="0",
            bot=False,
            avatar=None,
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            joined_at=None,
            roles=[],
        ),
        channel=SimpleNamespace(
            id=20,
            name="general",
            topic=None,
            type="text",
            position=1,
            guild=SimpleNamespace(id=10, name="Foresight", member_count=1),
        ),
        guild=SimpleNamespace(id=10, name="Foresight", member_count=1),
        reference=None,
        mentions=[],
        attachments=[],
        embeds=[],
    )


async def test_message_flows_from_event_to_api(tmp_path, monkeypatch):
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "test-token")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}")
    await init_db(engine)
    repo = Repository(engine)
    writer = Writer(repo, batch_size=10, flush_interval_ms=20)
    await writer.start()

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "translation": "登录总是超时",
                                        "sentiment": "negative",
                                        "sentiment_score": 0.8,
                                        "issues": ["登录超时"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        )
        analysis = AnalysisWorker(
            repo,
            LlmClient(
                AiConfig(
                    enabled=True,
                    base_url="https://llm.example/v1",
                    model="test-model",
                    api_key="key",
                )
            ),
            max_concurrency=1,
        )
        await analysis.start()

        handler = EventHandler(
            SimpleNamespace(
                discord=SimpleNamespace(channel_ids=(20,)),
                media=SimpleNamespace(backend="local"),
            ),
            writer,
            analysis_sink=analysis.submit,
        )
        handler.handle_message(_fake_message("login always times out"))
        await asyncio.sleep(0.8)
        await analysis.stop()
        await writer.stop()

    async with engine.connect() as conn:
        stored = (await conn.execute(select(messages))).one()
        analysed = (await conn.execute(select(message_analysis))).one()
    assert stored.content == "login always times out"
    assert stored.analysis_status == "done"
    assert analysed.sentiment == "negative"

    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "e2e.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'e2e.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-token"}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/messages", headers=headers, params={"q": "times out"})
        assert response.status_code == 200
        assert response.json()["total"] == 1
        stats = await client.get("/api/stats/overview", headers=headers)
        assert stats.json()["total_messages"] == 1
    await engine.dispose()
```

- [ ] **Step 2: 运行测试**

Run: `uv run pytest tests/integration/test_end_to_end.py -v`
Expected: PASS —— 1 passed

- [ ] **Step 3: 运行全量测试**

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 4: 提交**

```bash
git add tests/integration/test_end_to_end.py
git commit -m "test: end to end flow from event to API"
```

---

## M4 验收

```bash
uv run pytest -v                  # 全部通过
docker build -t foresight:latest .
docker compose config
./scripts/install_skill.sh --target-dir /tmp/skills --connection-file /tmp/connection.toml
ls /tmp/skills/foresight-analytics
```

四条命令都成功即表示交付完成。
