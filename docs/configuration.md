# 配置

`config.toml` 完整示例见 [`config.example.toml`](../config.example.toml)。这是唯一的配置模板：`.env` 里的 `COMPOSE_PROFILES` 决定 compose 启动哪些可选服务，非空的 `FORESIGHT_*` 环境变量覆盖同名设置，因此只靠 `.env` 也能决定整个部署架构。

| 段 | 关键项 | 说明 |
|---|---|---|
| `[discord]` | `channel_ids` | 白名单频道 ID 列表，非空 |
| `[database]` | `backend` | `sqlite` / `postgresql` / `mysql` |
| `[database]` | `path` | SQLite 文件路径，仅 `sqlite` 模式使用 |
| `[media]` | `backend` | `local`（默认）/ `s3` |
| `[media.s3]` | `bucket` / `endpoint_url` / `region` | S3 兼容存储；启用 `rustfs` profile 时桶会自动创建 |
| `[api]` | `host` / `port` | 默认 `127.0.0.1:8000` |
| `[api]` | `metrics_token` | 给 `/metrics` 加一道单独的令牌 |
| `[api]` | `metrics_refresh_seconds` | `/metrics` 抓取时数据库 gauge 的刷新间隔，默认 `30` 秒 |
| `[ai]` | `enabled` / `base_url` / `model` | OpenAI 兼容接口 |
| `[writer]` | `batch_size` / `flush_interval_ms` | 批量落库参数 |

**所有密钥只能走环境变量**，不要写进 `config.toml`：

| 环境变量 | 什么时候必须设置 | 说明 |
|---|---|---|
| `COMPOSE_PROFILES` | Docker 部署 | 逗号分隔的可选服务：`mysql` / `postgresql` / `rustfs`；留空为 SQLite + 本地附件 |
| `FORESIGHT_DATABASE_BACKEND`、`FORESIGHT_MEDIA_BACKEND` | 可选 | 应用使用的数据库 / 媒体后端；非空时覆盖 `config.toml` |
| `FORESIGHT_DATABASE_HOST` / `_PORT` / `_NAME` / `_USER` / `_PATH` | 可选 | 覆盖数据库连接信息 |
| `FORESIGHT_S3_BUCKET` / `_ENDPOINT_URL` / `_REGION` / `_PREFIX` / `_USE_PATH_STYLE` | 可选 | 覆盖 S3 连接信息 |
| `DISCORD_TOKEN` | 采集 / `backfill` 时 | Discord bot token |
| `DISCORD_CHANNEL_IDS` | 可选 | 逗号分隔的频道 ID，覆盖 `config.toml` 的 `[discord].channel_ids` |
| `FORESIGHT_API_TOKEN` | API 不在本机监听时 | API 的 Bearer token；不设置且绑定 `0.0.0.0` 时服务拒绝启动 |
| `FORESIGHT_LLM_API_KEY` | 开启 AI 分析时 | 模型服务的 key |
| `FORESIGHT_DB_PASSWORD` | PostgreSQL / MySQL 时 | 数据库密码 |
| `FORESIGHT_S3_ACCESS_KEY_ID` | `media.backend = "s3"` 时 | 对象存储的 access key；启用 `rustfs` profile 时同时也是 RustFS 根账号 |
| `FORESIGHT_S3_SECRET_ACCESS_KEY` | `media.backend = "s3"` 时 | 对象存储的 secret；启用 `rustfs` profile 时同时也是 RustFS 根密码 |
| `FORESIGHT_CONFIG` | 可选 | 配置文件路径，默认 `config.toml` |
| `FORESIGHT_API_HOST` / `FORESIGHT_API_PORT` | 可选 | 覆盖 `[api].host` / `[api].port` |

compose 另外用一个 `FORESIGHT_PORT` 决定宿主机上映射出来的端口（默认 `8000`），它只影响 compose，不影响服务本身。启用 `rustfs` profile 时还有 `RUSTFS_PORT` / `RUSTFS_CONSOLE_PORT`（默认 `9000` / `9001`）控制对象存储和控制台的宿主机端口。
