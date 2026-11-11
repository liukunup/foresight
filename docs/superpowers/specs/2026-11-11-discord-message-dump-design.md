# ForeSight — Discord 频道消息转储服务 设计文档

- 日期：2026-11-11
- 状态：待评审
- 分支：`dev`

## 1. 目标

构建一个常驻服务：只要用户在受监控的 Discord 频道发言，消息（及其编辑、删除、表情回应、附件）就被转储到数据库，供后续统计分析使用。配套提供：

1. HTTP API，让 AI 工具通过分页接口查询与聚合数据；
2. 一个可安装的 Agent Skill，让 AI 知道怎么查、查什么、怎么解读；
3. AI 分析子系统（翻译 / 情绪评估 / 问题提取）；
4. Prometheus 指标暴露；
5. Docker 镜像与 docker-compose 编排；
6. 简易使用文档。

## 2. 非目标

明确不做，避免范围蔓延：

- **不做历史回填到"全自动"**：补录历史必须由用户显式执行 `backfill` 命令，服务启动时不自动拉历史。
- **不做消息搜索服务**：检索由 AI 通过 `/api/query` 自由 SQL 完成，不自建倒排索引。
- **不做多租户 / 多账号**：单个 token、单个机器人。
- **不做媒体二次处理**：不生成缩略图、不转码、不抽帧、不做 OCR。
- **不做消息编辑历史留存**：只保留编辑后的最新内容。
- **不做 Web 前端面板**：交互入口是 CLI + HTTP API + Skill。

## 3. 术语

| 术语 | 含义 |
|---|---|
| 实时路径 | 服务运行期间由 Discord Gateway 推送的事件写入 |
| 补录路径 | 用户显式执行 `backfill` 命令从 Discord REST 历史 API 拉取写入 |
| 白名单频道 | 配置在 `discord.channel_ids` 中的频道，只有这些频道的消息会被捕获 |
| 领域对象 | `domain/models.py` 中的纯 dataclass，与 Discord SDK 对象解耦 |
| 软删除 | 消息被删除时只写 `deleted_at` 时间戳，保留原文 |
| 软移除 | 表情被移除时只写 `removed_at` 时间戳，保留记录 |
| 只读 SQL 端点 | `/api/query`，仅允许单条 SELECT/WITH 语句，引擎层拦截写操作 |

## 4. 架构总览

```
                        ┌───────────────────────────────┐
 Discord Gateway ──────▶│ bot.py  (事件注册 + 白名单过滤) │
                        └───────────────┬───────────────┘
                                        │
                        ┌───────────────▼───────────────┐
                        │ adapters/discord_mapper.py      │
                        │ Discord 对象 → 领域 dataclass   │
                        └───────────────┬───────────────┘
                                        │
                        ┌───────────────▼───────────────┐
                        │ writer.py  (asyncio.Queue)      │
                        │  单写协程 / 批量事务落库         │
                        └───────┬───────────────┬────────┘
                                │               │
              ┌─────────────────▼──────┐  ┌─────▼──────────────┐
              │ analysis/worker.py     │  │ media_worker.py     │
              │ LLM: 翻译/情绪/问题提取  │  │ 附件下载 → 本地/S3   │
              └────────┬───────────────┘  └─────┬──────────────┘
                       │                        │
        ┌──────────────▼────────────────────────▼──────────────┐
        │ storage/  db.py · tables.py · repository.py · backend │
        │            media.py (MediaStore: local | s3)          │
        └──────────────┬────────────────────────┬─────────────┘
                       │                        │
         ┌─────────────▼─────────┐   ┌──────────▼────────────┐
         │ storage/ (SQLite/PG/MySQL)│   │ api/ (FastAPI)       │
         │                          │   │  /api/*  +  /metrics  │
         └─────────────────────────┘   └───────────────────────┘
```

依赖方向单向，无环：

- `bot → adapters → domain`（`domain` 零依赖，纯 dataclass）
- `bot / writer / analysis / media_worker → storage`
- `api → storage`
- `cli → bot / api / storage`

### 4.1 目录结构

```
foresight/
├── pyproject.toml
├── config.example.toml
├── .env.example
├── README.md
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── docs/superpowers/specs/2026-11-11-discord-message-dump-design.md
├── scripts/
│   └── install_skill.sh
├── skills/foresight-analytics/
│   ├── SKILL.md
│   ├── reference/
│   │   ├── api.md
│   │   ├── schema.md
│   │   └── queries.md
│   └── scripts/query.py
├── src/foresight/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── logging_setup.py
│   ├── bot.py
│   ├── backfill.py
│   ├── writer.py
│   ├── media_worker.py
│   ├── cli.py
│   ├── domain/
│   │   ├── __init__.py
│   │   └── models.py
│   ├── adapters/
│   │   ├── __init__.py
│   │   └── discord_mapper.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py
│   │   ├── tables.py
│   │   ├── repository.py
│   │   ├── backend.py
│   │   └── media.py
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── llm.py
│   │   ├── prompts.py
│   │   └── worker.py
│   └── api/
│       ├── __init__.py
│       ├── app.py
│       ├── deps.py
│       ├── metrics.py
│       └── routers/
│           ├── health.py
│           ├── messages.py
│           ├── users.py
│           ├── channels.py
│           ├── reactions.py
│           ├── stats.py
│           └── query.py
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/
    └── fixtures/
```

### 4.2 为什么这样分层

- **Discord SDK 字段名只出现在 `adapters/discord_mapper.py`**。领域模型用中性命名（`author_id`、`attachment_count`、`attachments_json`），换平台（Slack/Telegram）或写单测时不需要 discord.py 对象。
- **`repository` 是唯一懂 SQL 的地方**。CLI（给人用）与 API（给 AI 用）都走它，两条路径不会出现统计口径不一致。
- **写库不阻塞事件循环**。Discord 回调里同步写库会让磁盘 IO 阻塞其它事件处理，队列把写入移出回调路径。

## 5. 数据模型

### 5.1 通用约定

- Discord 雪花 ID 存 `BIGINT`（< 2^63，安全且可索引）
- 时间统一存 **UTC ISO-8601 TEXT**，例：`2026-11-11T13:45:00+00:00`。SQLite 的 `date()`/`strftime()`、PG 的 `to_char()`、MySQL 的 `DATE_FORMAT()` 都能处理
- JSON 字段一律用 `TEXT` 存 JSON 字符串，序列化在应用层做（不引入 PG `JSONB` 等方言专属类型，保证三方言行为一致）
- 布尔列用 SQLAlchemy `Boolean`（SQLite 存 0/1，PG 原生 bool，MySQL TINYINT）

### 5.2 表结构

**guilds**

| 列 | 类型 | 说明 |
|---|---|---|
| `guild_id` | BIGINT PK | 服务器 ID |
| `name` | TEXT | 服务器名 |
| `member_count` | INTEGER | 成员数快照 |
| `captured_at` | TEXT | 快照时间 |

**channels**

| 列 | 类型 | 说明 |
|---|---|---|
| `channel_id` | BIGINT PK | 频道 ID |
| `guild_id` | BIGINT | 所属服务器 |
| `name` | TEXT | 频道名 |
| `topic` | TEXT | 话题 |
| `channel_type` | TEXT | 频道类型 |
| `position` | INTEGER | 排序位 |
| `captured_at` | TEXT | 快照时间 |

**users**

| 列 | 类型 | 说明 |
|---|---|---|
| `user_id` | BIGINT PK | 用户 ID |
| `username` | TEXT | 用户名 |
| `global_name` | TEXT | 展示名 |
| `display_name` | TEXT | 服务器内昵称 |
| `discriminator` | TEXT | 旧式标签（历史消息可能带） |
| `avatar_url` | TEXT | 头像 |
| `is_bot` | BOOLEAN | 是否机器人 |
| `account_created_at` | TEXT | 账号注册时间 |
| `guild_joined_at` | TEXT | 加入服务器时间（无 members intent 时为 NULL） |
| `roles_json` | TEXT | 角色 ID 数组 |
| `updated_at` | TEXT | 快照更新时间 |

**messages**（主表）

| 列 | 类型 | 说明 |
|---|---|---|
| `message_id` | BIGINT PK | 消息 ID（全局唯一） |
| `guild_id` | BIGINT | 服务器 |
| `channel_id` | BIGINT | 频道（NOT NULL） |
| `author_id` | BIGINT | 作者（NOT NULL） |
| `content` | TEXT | 正文（编辑后为最新内容） |
| `message_type` | INTEGER | Discord 消息类型 |
| `created_at` | TEXT | 发送时间 |
| `captured_at` | TEXT | 入库时间 |
| `edited_at` | TEXT | 最近编辑时间，NULL 表示未编辑 |
| `edit_count` | INTEGER | 编辑次数，默认 0 |
| `deleted_at` | TEXT | 软删除时间，NULL 表示未删除 |
| `reply_to_message_id` | BIGINT | **被回复消息的 ID**（只存 ID，不存内容快照） |
| `mentions_count` | INTEGER | @提及数 |
| `char_count` | INTEGER | 正文字符数 |
| `word_count` | INTEGER | 正文词数 |
| `attachment_count` | INTEGER | 附件数 |
| `attachments_json` | TEXT | 附件摘要 JSON 数组 |
| `embeds_json` | TEXT | 嵌入内容 JSON |
| `is_bot` | BOOLEAN | 作者是否机器人（冗余，便于查询） |
| `analysis_status` | TEXT | `pending` / `done` / `failed` / `skipped` |

**reactions**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK AI | 自增 |
| `message_id` | BIGINT NOT NULL | 目标消息 |
| `user_id` | BIGINT NOT NULL | 加表情的人 |
| `emoji` | TEXT NOT NULL | unicode 字符或自定义 emoji 名 |
| `emoji_id` | BIGINT | 自定义 emoji 的 snowflake |
| `is_custom` | BOOLEAN | 是否自定义 emoji |
| `added_at` | TEXT | 加入时间 |
| `removed_at` | TEXT | 软移除时间，NULL 表示仍在 |

唯一约束 `(message_id, user_id, emoji)`。

**attachments**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK AI | 自增 |
| `message_id` | BIGINT NOT NULL | 所属消息 |
| `attachment_id` | BIGINT | Discord 附件 ID |
| `filename` | TEXT | 原文件名 |
| `content_type` | TEXT | MIME |
| `size` | BIGINT | 字节数 |
| `url` | TEXT | 原始 Discord CDN 链接 |
| `sha256` | TEXT | 下载后计算，用于去重与校验 |
| `storage_backend` | TEXT | `local` / `s3` |
| `storage_key` | TEXT | 存储 key |
| `storage_uri` | TEXT | 可访问 URI（`file://` 或 `s3://`） |
| `status` | TEXT | `pending` / `stored` / `failed` |
| `error` | TEXT | 失败原因 |
| `captured_at` | TEXT | 入库时间 |
| `stored_at` | TEXT | 存储完成时间 |

**message_analysis**

| 列 | 类型 | 说明 |
|---|---|---|
| `message_id` | BIGINT PK | 一条消息一行 |
| `translated_text` | TEXT | 译文 |
| `sentiment` | TEXT | `positive` / `neutral` / `negative` / `mixed` |
| `sentiment_score` | REAL | 0–1 置信度 |
| `issues_json` | TEXT | 提取出的问题/反馈点数组 |
| `model` | TEXT | 使用的模型 |
| `prompt_version` | TEXT | prompt 版本号 |
| `latency_ms` | INTEGER | 调用耗时 |
| `error` | TEXT | 失败原因 |
| `analyzed_at` | TEXT | 分析完成时间 |

### 5.3 索引

```
messages(channel_id, created_at)
messages(author_id, created_at)
messages(guild_id, created_at)
messages(created_at)
messages(analysis_status)
reactions(message_id)
attachments(message_id)
attachments(status)
```

### 5.4 外键策略

`reactions` 与 `attachments` **不声明外键到 `messages`**，只建索引。原因：机器人启动前就存在的消息，可能在服务运行期间被人加表情或被引用，此时 `messages` 表里没有对应行，强外键会导致写入失败。

### 5.5 关键语义（分析口径）

1. **编辑只保留最新**：`on_raw_message_edit` 更新 `content` 与 `edited_at`，`edit_count += 1`，**不留旧内容**。
2. **软删除**：`deleted_at` 打时间戳，`content` 保留原文 → 可分析"曾发出后被删除的消息占比"。
3. **软移除表情**：`removed_at` 打戳而非删行，有效表情数 = `removed_at IS NULL` → 避免"加了一万个表情又全撤了"污染统计。
4. **快照随消息刷新**：每条消息到达时 upsert 对应的 user / channel / guild 快照（改名/换头像能跟上）。
5. **回复关系只存 ID**：`reply_to_message_id` 指向被回复消息，需要详情时按 ID 关联查询。
6. **补录不覆盖实时**：补录走 `insert_only` 策略（见 §8）。
7. **字数口径**：`char_count` = `len(content)`（Unicode 码点数）；`word_count` = 连续非空白字符段数 + CJK 字符个数（每个汉字/假名各算一个词），保证中英文都有可用的"长度"指标。
8. **`analysis_status` 初始值**：实时捕获的消息初始为 `pending`；`ai.enabled=false` 时直接置 `skipped`。补录消息一律 `skipped`（见 §8）。

## 6. Discord 事件处理

### 6.1 事件清单

统一使用 `on_raw_*` 系列：

| 事件 | 动作 |
|---|---|
| `on_message` | 插入 `messages`；upsert user/channel/guild 快照；记录 `reply_to_message_id`；写 `attachments` 待下载行 |
| `on_raw_message_edit` | 更新 `content`、`edited_at`、`edit_count += 1` |
| `on_raw_message_delete` | `deleted_at = now`，`content` 保留 |
| `on_raw_reaction_add` | upsert `reactions`（`removed_at = NULL`） |
| `on_raw_reaction_remove` | `removed_at = now` |

**为什么用 raw 事件**：`on_message_edit` / `on_message_delete` 只在消息仍留在内存缓存时触发。缓存默认只保留最近几百条，频道活跃时旧消息会被挤出去，事件静默丢失。`on_raw_message_edit` / `on_raw_message_delete` 不依赖缓存，payload 携带 `channel_id` / `message_id`，覆盖完整。

### 6.2 Intent 要求

必需：`Intents.messages`、`Intents.message_content`（**特权 intent，需在 Discord 开发者后台开启**）、`Intents.reactions`、`Intents.guilds`。

可选：`Intents.members` —— 开启后才能拿到 `guild_joined_at`，否则该列为 NULL。

### 6.3 白名单过滤

`config.discord.channel_ids` 在启动时读入内存 `set`。handler 第一步判断 `message.channel.id in whitelist`，不在则直接返回，不做任何转换。

## 7. 写入路径

### 7.1 队列与批处理

```
handler → writer.submit(record)   # put_nowait，不阻塞
writer 后台协程：
    每 50ms 或攒满 100 条 → 单事务 executemany 落库
```

- 队列有界（默认 10000）。**队列满时丢弃并记 WARNING + 计指标，绝不阻塞 event loop**（阻塞 event loop = 整个 bot 掉线）
- 所有写入走幂等 upsert（`INSERT ... ON CONFLICT DO UPDATE`），重复投递同一条 `message_id` 不产生脏数据
- 实时路径的 upsert **只更新** `content` / `edited_at` / `edit_count` / `deleted_at`，不触碰 `analysis_status` / `captured_at`

### 7.2 失败处理

整批重试 3 次（指数退避 1s / 2s / 4s）→ 仍失败则把这批原始记录以 JSONL 落到 `data/failed_batch.jsonl` 并继续运行；进程重启时自动读取并补写。

## 8. 补录命令（backfill）

```
foresight backfill --days 7                     # 补最近 7 天
foresight backfill --since 2026-11-01           # 补到指定日期（含）为止
foresight backfill --days 7 --channel 123456    # 只补某个频道
foresight backfill --days 7 --limit 20000       # 单频道上限
```

- `--days` 与 `--since` **互斥，必须且只能给一个**（默认 `--days 7`）
- **独立进程、独立连接 Discord**：backfill 是单独的 CLI 进程，自己建 discord.py 客户端，只拉历史、不注册实时监听。它复用同一套 mapper / writer / storage 代码路径（`mapper → writer → 库`），所以快照、去重、字段口径与实时完全一致
- 迭代 `channel.history(limit=..., before=...)` 逐页拉取，discord.py 自带速率限制处理
- 每 1000 条打印一次进度（频道名 + 已拉条数 + 游标时间）
- 补录消息的 `analysis_status = 'skipped'`（不跑 LLM）
- **不补表情**（对每条消息单独 GET reactions，N 条消息 = N 次请求，不可行）
- 补录消息若有 `edited_at` 则填入，但 `edit_count` 保持 0（Discord 不提供修改次数）

### 8.1 写入策略区分

| 路径 | 策略 | 行为 |
|---|---|---|
| 实时 | `upsert`（DO UPDATE） | 只更新 `content` / `edited_at` / `edit_count` / `deleted_at` |
| 补录 | `insert_only`（DO NOTHING） | 已存在的消息原样保留，绝不用历史快照覆盖实时捕获的数据 |

## 9. 媒体存储

### 9.1 抽象接口

```python
class MediaStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str: ...
    async def exists(self, key: str) -> bool: ...
    async def open(self, key: str) -> AsyncIterator[bytes]: ...
```

### 9.2 两个实现

- `LocalMediaStore(root="data/media")` —— 默认。写入 `data/media/YYYY/MM/DD/<attachment_id>.<ext>`，返回 `file://` URI
- `S3MediaStore(bucket, endpoint_url, region, access_key_id, secret_access_key, prefix, use_path_style)` —— 兼容 AWS S3 / MinIO / Cloudflare R2 / 阿里云 OSS（S3 协议），返回 `s3://bucket/<key>` URI

**key 统一用 `attachment.id` 命名**（不用原文件名），避免重名覆盖和非法字符；原文件名另存字段。日期分目录避免单目录文件过多。

### 9.3 下载流程

消息入库时插入 `status='pending'` 的附件行 → media worker（默认并发 3）逐个下载 → 写入本地或上传 S3 → 更新 `status='stored'`、`sha256`、`storage_key`、`storage_uri`。失败重试 3 次后标 `failed` 并记 `error`。

超过 `max_download_size_mb` 的附件跳过下载（状态标 `skipped`，保留 URL）。

**只存原文件**，不生成缩略图、不转码、不抽帧。

## 10. AI 分析子系统

### 10.1 模型调用

走 OpenAI 兼容接口（`POST {base_url}/chat/completions`，`response_format={"type":"json_object"}`）。`base_url` 可指向 OpenAI、DeepSeek、Kimi、Ollama、vLLM 等。

**一次调用产出三个结果**，避免为翻译/情绪/问题分别调三次：

```json
{
  "translation": "译后文本",
  "sentiment": "negative",
  "sentiment_score": 0.82,
  "issues": ["希望增加导出功能", "登录经常超时"]
}
```

- `sentiment` 固定枚举：`positive` / `neutral` / `negative` / `mixed`
- `sentiment_score`：0–1 置信度
- `issues`：问题/反馈点数组，无则空数组
- `translation`：目标语言由 `ai.translation_target` 决定（默认 `zh-CN`）

### 10.2 流程

```
writer 落库成功 → 消息入分析队列 → worker（并发 max_concurrency，默认 2）调 LLM
                → upsert message_analysis → messages.analysis_status = 'done'
```

### 10.3 状态与自愈

`messages.analysis_status` 取值 `pending` / `done` / `failed` / `skipped`。

- 进程重启时扫描 `analysis_status='pending'` 的消息重新入队
- 失败重试 3 次；仍失败则置 `failed` 并记录 `error`
- LLM 返回非 JSON 时，用更严格的 prompt 重试一次；仍失败置 `failed`

### 10.4 跳过规则（可配）

空内容、纯 emoji、纯 URL、长度 < 2 的消息直接置 `analysis_status='skipped'`，不调模型。

### 10.5 密钥

`api_key` 只从环境变量 `FORESIGHT_LLM_API_KEY` 读取，不写进配置文件、不入库、不进日志。

## 11. HTTP API

### 11.1 基础

- 前缀 `/api`
- 认证：`Authorization: Bearer <token>`，token 从环境变量 `FORESIGHT_API_TOKEN` 读取
- 默认监听 `127.0.0.1:8000`（`api.host` / `api.port` 可配）
- **若绑定到非本机地址而未设置 token，服务直接拒绝启动**
- FastAPI 自动生成 `/docs`（OpenAPI），AI 可自行读取接口定义

### 11.2 数据库连接

API 进程用 **SQLite 只读 URI**（`file:...?mode=ro`）打开数据库，从机制上保证 API 永不写库。

代价：API 必须与 DB 位于同一文件系统（WAL 模式需要写 `-wal` / `-shm` 文件）。Docker 单容器场景天然满足。

PG / MySQL 下使用只读事务（`SET TRANSACTION READ ONLY` / `SET SESSION TRANSACTION READ ONLY`）。

### 11.3 分页信封

```json
{ "total": 1234, "limit": 50, "offset": 0, "has_more": true, "items": [] }
```

`limit` 默认 50，上限 500。

**过滤参数语义**（`GET /api/messages`）：

| 参数 | 语义 |
|---|---|
| `channel_id` / `author_id` / `guild_id` | 精确匹配 ID |
| `since` / `until` | ISO-8601 时间戳，闭区间 `[since, until]`，按 `created_at` 比较 |
| `q` | **大小写不敏感**的正文包含匹配，实现为 `lower(content) LIKE lower('%q%')`（三方言一致） |
| `min_len` | `char_count >= min_len` |
| `has_attachment` | `true` → `attachment_count > 0`；`false` → `attachment_count = 0` |
| `deleted` | `true` → `deleted_at IS NOT NULL`；`false` → `deleted_at IS NULL` |
| `edited` | `true` → `edited_at IS NOT NULL`；`false` → `edited_at IS NULL` |
| `order` | `asc` / `desc`，按 `created_at`（默认 `desc`）；同时间戳时以 `message_id` 作为次级排序键，保证分页稳定 |

### 11.4 端点

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 状态、消息总数、DB 大小、最后入库时间 |
| `GET /api/messages` | 过滤：`channel_id` `author_id` `guild_id` `since` `until` `q`（内容 LIKE）`min_len` `has_attachment` `deleted` `edited` `order` |
| `GET /api/messages/{id}` | 单条消息 + 关联的 AI 分析结果 |
| `GET /api/users` | 发言用户列表 + 每人发言量（可按频道过滤） |
| `GET /api/users/{id}` | 用户档案 + 活跃度 |
| `GET /api/channels` | 频道列表 + 各频道消息量 |
| `GET /api/reactions` | 表情明细，可按 `message_id` / 频道过滤 |
| `GET /api/stats/overview` | 总量、独立发言人数、平均字数、删除率、编辑率、日活 |
| `GET /api/stats/top-talkers` | 发言排行 |
| `GET /api/stats/activity` | 时间分布（`bucket=hour\|day`） |
| `GET /api/stats/topics` | 高频词/议题（优先聚合 `message_analysis.issues`，无分析数据时回退到内容分词） |

### 11.5 只读 SQL 端点

`POST /api/query`

```json
{
  "sql": "SELECT author_id, COUNT(*) c FROM messages WHERE created_at >= :since GROUP BY 1 ORDER BY c DESC",
  "params": { "since": "2026-11-01" },
  "limit": 100,
  "offset": 0
}
```

响应：

```json
{ "columns": ["author_id", "c"], "rows": [[1, 10]], "row_count": 1, "elapsed_ms": 3 }
```

安全约束：

1. 用 SQLite 的 `set_authorizer` 做**引擎层只读授权**（拒绝任何写入 / 改 schema / attach），而不是字符串黑名单 —— 黑名单能被 `WITH ... DELETE`、注释绕过
2. SQL 必须是单条语句（`sqlite3.complete_statement` 校验 + 只允许一个结尾分号）
3. 无 LIMIT 时自动追加 `LIMIT limit`
4. PG / MySQL 下改用只读事务 + 多语句禁用实现同等约束

**方言提示**：自由 SQL 的语法跟随所选数据库（SQLite 的 `strftime` 在 PG 上不通用）。这一点在 API 文档与 Skill 参考文档中明确标注。

### 11.6 统计查询的实现方式

统计端点**不手写 SQL**，用 SQLAlchemy Core 表达式构建（日期分桶等由方言编译器翻译），三方言只维护一份代码。

## 12. Prometheus 指标

每个进程暴露自己的 `/metrics`，Prometheus 抓多个 target。

- `both` 模式（单进程跑 bot + API）：FastAPI app 上挂 `/metrics`，bot 侧与 API 侧指标在同一个 registry，一个端口 8000 全搞定
- 分离部署：bot 容器 `foresight run --metrics-port 9100`，API 容器 `foresight serve`（8000）；Prometheus 配两个 job

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `foresight_messages_captured_total` | Counter | `channel_id`, `guild_id` | 捕获的消息数 |
| `foresight_messages_deleted_total` | Counter | `channel_id` | 软删除的消息数 |
| `foresight_messages_edited_total` | Counter | `channel_id` | 编辑过的消息数 |
| `foresight_reactions_total` | Counter | `channel_id` | 收到的表情回应数 |
| `foresight_backfill_messages_total` | Counter | `channel_id` | 补录条数 |
| `foresight_writer_queue_depth` | Gauge | — | 写入队列积压 |
| `foresight_writer_batches_total` | Counter | `status` | 落库批次数（`ok`/`failed`） |
| `foresight_writer_batch_size` | Histogram | — | 每批条数 |
| `foresight_ai_requests_total` | Counter | `status` | LLM 调用数（`ok`/`failed`） |
| `foresight_ai_request_duration_seconds` | Histogram | — | LLM 耗时 |
| `foresight_ai_tokens_total` | Counter | `type` | `prompt` / `completion` token 数 |
| `foresight_ai_queue_depth` | Gauge | — | 分析队列积压 |
| `foresight_media_downloads_total` | Counter | `status` | 附件下载数（`ok`/`failed`） |
| `foresight_media_bytes_total` | Counter | — | 下载字节数 |
| `foresight_api_requests_total` | Counter | `method`, `path`, `status` | API 请求数 |
| `foresight_api_request_duration_seconds` | Histogram | `method`, `path` | API 耗时 |
| `foresight_db_messages_total` | Gauge | — | DB 中消息总数 |
| `foresight_db_attachments_total` | Gauge | — | DB 中附件总数 |

**标签基数纪律**：`channel_id` / `guild_id` 可用（数量有限）；**`user_id` 绝不能做 label**（消息量一大就是几十万时间序列，会打爆 Prometheus）。用户维度通过 `foresight_db_*` 或直接查 API 获取。

`/metrics` 默认不认证（靠内网隔离）；设置 `api.metrics_token` 后才校验。

## 13. CLI 命令参考

| 命令 | 说明 |
|---|---|
| `foresight run` | 启动机器人（可选 `--metrics-port`） |
| `foresight serve` | 只启动 API（可选 `--host` / `--port` / `--metrics-port`） |
| `foresight both` | 单进程内同时跑机器人与 API（默认 docker compose 用） |
| `foresight backfill` | 补录历史（见 §8） |
| `foresight query "SQL"` | 对数据库执行只读查询并以表格/JSON 输出 |
| `foresight stats` | 常用统计概览 |
| `foresight analyze` | 对 `analysis_status='pending'` 的消息触发 AI 分析（可加 `--since` / `--limit`） |
| `foresight init-db` | 建表 / 迁移数据库结构 |

## 14. 配置项

```toml
[discord]
channel_ids = [123456789012345678]

[database]
backend = "sqlite"            # sqlite | postgresql | mysql
path = "data/foresight.db"
# postgresql / mysql 专用
host = "localhost"
port = 5432
database = "foresight"
user = "foresight"

[media]
backend = "local"              # local | s3
local_root = "data/media"
max_download_size_mb = 100
download_concurrency = 3

[media.s3]
bucket = ""
endpoint_url = ""               # MinIO/R2: http://localhost:9000
region = "auto"
prefix = "foresight"
use_path_style = true

[api]
host = "127.0.1"
port = 8000
metrics_token = ""              # 留空则 /metrics 不认证

[ai]
enabled = true
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
translation_target = "zh-CN"
max_concurrency = 2
timeout_seconds = 30

[writer]
batch_size = 100
flush_interval_ms = 50
queue_maxsize = 10000

[logging]
level = "INFO"
```

### 14.1 环境变量（所有密钥）

| 变量 | 用途 |
|---|---|
| `DISCORD_TOKEN` | **必需**，Discord bot token |
| `FORESIGHT_API_TOKEN` | 对外暴露 API 时**必需** |
| `FORESIGHT_LLM_API_KEY` | 启用 AI 分析时**必需** |
| `FORESIGHT_DB_PASSWORD` | PG / MySQL 密码 |
| `FORESIGHT_S3_ACCESS_KEY_ID` | S3 access key |
| `FORESIGHT_S3_SECRET_ACCESS_KEY` | S3 secret key |

配置文件里不出现任何密钥。

**环境变量覆盖配置**：以下环境变量优先级高于 `config.toml` 的同名项（docker-compose 用这种方式避免改只读挂载的配置文件）：

| 环境变量 | 覆盖 |
|---|---|
| `FORESIGHT_API_HOST` | `api.host` |
| `FORESIGHT_API_PORT` | `api.port` |
| `FORESIGHT_CONFIG` | 配置文件路径本身（默认 `config.toml`） |

## 15. Docker

### 15.1 Dockerfile

多阶段构建：

- `builder`：`python:3.13-slim` + `uv`，依赖装到 `/venv`
- `runtime`：`python:3.13-slim`，非 root 用户（uid 10001），COPY `/venv` + `src/`，`ENTRYPOINT ["foresight"]`
- `HEALTHCHECK` 调 `GET /api/health`

三个入口：`foresight run` / `foresight serve` / `foresight both`。

`both` 在**单进程内**用 `asyncio.gather` 同跑机器人与 API —— SQLite 只有一个进程在写，不会出现双进程写竞争。

### 15.2 docker-compose.yml

```yaml
services:
  foresight:
    build: .
    image: foresight:latest
    command: ["both"]
    ports:
      - "8000:8000"          # 默认 8000，可用 .env 覆盖
    env_file: [".env"]
    environment:
      FORESIGHT_API_HOST: "0.0.0.0"
      FORESIGHT_API_PORT: "8000"
    volumes:
      - ./config.toml:/app/config.toml:ro
      - ./data:/app/data      # 数据库 + 媒体文件持久化
    restart: unless-stopped
```

`.env` 存放所有密钥。`./data` 卷同时承载 SQLite 文件与本地媒体。

因为 compose 里 API 监听 `0.0.0.0`（非本机地址），**`.env` 必须包含 `FORESIGHT_API_TOKEN`**，否则按 §11.1 的规则服务拒绝启动。`.env.example` 提供模板。

## 16. Agent Skill

### 16.1 结构

```
skills/foresight-analytics/
├── SKILL.md                # 主指南：何时用、怎么调、常见分析配方
├── reference/
│   ├── api.md              # 端点清单、参数、分页信封、认证
│   ├── schema.md           # 表结构速查（字段语义、软删除/软移除口径）
│   └── queries.md          # 预置分析 SQL 模板（按方言分组）
└── scripts/query.py        # 小封装：读连接信息 → 带 token 调 API → 格式化输出
```

### 16.2 SKILL.md 骨架

1. **前置检查**：读 `~/.config/foresight/connection.toml` → 取 `api_base_url`、`token_env`、`db_path`；`GET /api/health` 探活，API 不在就提示先起服务
2. **调用方式**：`curl` 示例（GET 端点）+ Python 示例（POST `/api/query`）
3. **常见分析配方**：概览 / 发言排行 / 负面情绪 + 问题提取 / 活跃时段热力 / 议题分布 / 频道对比
4. **输出规范**：给用户看**结论 + 关键数字 + 下一步建议**，不直接倒 SQL 原始结果
5. **边界**：只读；分页要跟着 `has_more` 翻页；SQL 方言跟随所选数据库

### 16.3 安装脚本

`scripts/install_skill.sh`：

- 目标 agent 目录：opencode → `~/.config/opencode/skills/`，Claude Code → `~/.claude/skills/`（`--target` 可指定）
- 复制 skill 目录，并生成 `~/.config/foresight/connection.toml`：

```toml
api_base_url = "http://127.0.0.1:8000"
token_env = "FORESIGHT_API_TOKEN"
db_path = "/abs/path/to/data/foresight.db"
```

- 提示用户 `export FORESIGHT_API_TOKEN=...`
- 支持 `--symlink` 用软链代替复制（开发时改代码即时生效）

## 17. 使用文档（README.md）

结构：

1. 项目简介
2. 快速开始 —— 两条路径：本地 `uv` 运行 / `docker compose` 运行
3. **Discord 机器人准备步骤**：建应用 → 开启 MESSAGE CONTENT INTENT → 邀请 bot → 抄频道 ID
4. 配置说明表
5. 命令参考（`run` / `serve` / `both` / `backfill` / `query` / `stats` / `analyze` / `init-db`）
6. API 使用说明（认证、分页信封、端点表、自由 SQL 的方言注意事项）
7. Skill 安装与用法
8. 常见问题：Intent 未开启、SQLite locked、Discord 限流、S3 权限不足、LLM 返回非 JSON、端口占用

## 18. 错误处理

原则：**单个事件失败绝不影响 bot 存活**。

| 环节 | 策略 |
|---|---|
| Discord 事件处理 | try/except 兜住，记 ERROR 后继续；不向 gateway 抛异常 |
| 写入失败 | 整批重试 3 次（1s/2s/4s）→ 落 `data/failed_batch.jsonl` → 重启自动补写 |
| 队列满 | 丢弃该条 + WARNING + 计指标，不阻塞 event loop |
| LLM 调用失败 | 重试 3 次 → `analysis_status=failed` + 错误信息 |
| LLM 返回非 JSON | 用更严格的 prompt 重试一次，仍失败则标 `failed` |
| 媒体下载失败 | 重试 3 次 → `status=failed` + 错误信息 |
| API 认证失败 | 401 |
| SQL 非法 / 非只读 | 400 |
| 参数错误 | 422（FastAPI 默认） |
| 数据库错误 | 500，错误体带可读 message |

## 19. 测试策略

pytest + pytest-asyncio，外部依赖全部 mock。

### 19.1 单元测试

- **mapper**：用 `SimpleNamespace` 构造假 Discord 对象，断言领域对象字段映射正确（不连 Discord）
- **统计逻辑**：临时 SQLite 灌 fixture 数据，断言 `overview` / `top-talkers` / `activity` 结果
- **`/api/query` 只读保护**：必须能拒绝 `DROP TABLE`、`INSERT`、`WITH x AS (DELETE ...)` 三类绕过
- **backfill 参数解析**：时间范围计算、`--limit` 上限
- **配置解析**：环境变量注入、密钥缺失时报错

### 19.2 集成测试

- FastAPI `TestClient` + 临时库：分页、过滤、统计端点
- writer：写入 → 落库 → 读回校验
- 媒体存储：本地实现的 put/exists/open 往返

### 19.3 mock 策略

| 依赖 | 方式 |
|---|---|
| Discord | 完全不测，靠 mapper 单测覆盖 |
| LLM (httpx) | `respx` |
| S3 | `moto` 或假实现 |

### 19.4 三方言一致性

同一份 fixture 数据分别写入 sqlite / postgres / mysql，断言统计结果一致。标记为可选测试（`@pytest.mark.dialects`），需要 docker 起 pg/mysql 时才跑；默认 CI 只跑 SQLite。

## 20. 依赖清单

**运行时**：`discord.py>=2.7`、`SQLAlchemy[asyncio]>=2.0`、`aiosqlite`、`asyncpg`（PG）、`asyncmy`（MySQL）、`fastapi`、`uvicorn`、`httpx`、`aioboto3`、`prometheus-client>=0.20`、`typer`、`rich`、`pydantic>=2`、`pydantic-settings`、`tomli`（<3.11 兜底，3.13 用内置 `tomllib`）

**开发**：`pytest`、`pytest-asyncio`、`pytest-cov`、`respx`、`moto[server]`

## 21. 风险与已知限制

1. **`message_content` 是特权 intent**：未在开发者后台开启则 `content` 恒为空。README 必须显著标注这一步。
2. **Discord 事件丢失**：网关断连期间的短暂离线、Discord 服务端限流导致的丢弃，无法保证零丢失。日志会记录 gateway 重连。
3. **自由 SQL 端点的方言差异**：SQLite / PG / MySQL 语法不通用，Skill 文档按方言给示例。
4. **API 只读连接要求同文件系统**：PG / MySQL 无此限制；SQLite 的 WAL 模式下，跨机器挂载 NFS 不安全。
5. **LLM 成本**：全量分析是逐条调用，成本随消息量线性增长。默认只在实时路径分析，补录默认跳过。
6. **高基数标签**：`channel_id` 数量有限可控；**绝不能用 `user_id` 做指标标签**。
7. **`/api/query` 的性能**：用户可写任意 SELECT，大表全表扫描会拖慢 SQLite 读锁。生产上建议限制 `limit` 上限并在 PG 上使用只读副本。
