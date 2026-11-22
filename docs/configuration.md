# 配置

`config.toml` 完整示例见 [`config.example.toml`](../config.example.toml)。

| 段 | 关键项 | 说明 |
|---|---|---|
| `[discord]` | `channel_ids` | 白名单频道 ID 列表，非空 |
| `[database]` | `backend` | `sqlite` / `postgresql` / `mysql` |
| `[media]` | `backend` | `local`（默认）/ `s3` |
| `[api]` | `host` / `port` | 默认 `127.0.0.1:8000` |
| `[api]` | `metrics_token` | 给 `/metrics` 加一道单独的令牌 |
| `[api]` | `metrics_refresh_seconds` | `/metrics` 抓取时数据库 gauge 的刷新间隔，默认 `30` 秒 |
| `[ai]` | `enabled` / `base_url` / `model` | OpenAI 兼容接口 |
| `[writer]` | `batch_size` / `flush_interval_ms` | 批量落库参数 |

**所有密钥只能走环境变量**，不要写进 `config.toml`：

| 环境变量 | 什么时候必须设置 | 说明 |
|---|---|---|
| `DISCORD_TOKEN` | 采集 / `backfill` 时 | Discord bot token |
| `FORESIGHT_API_TOKEN` | API 不在本机监听时 | API 的 Bearer token；不设置且绑定 `0.0.0.0` 时服务拒绝启动 |
| `FORESIGHT_LLM_API_KEY` | 开启 AI 分析时 | 模型服务的 key |
| `FORESIGHT_DB_PASSWORD` | PostgreSQL / MySQL 时 | 数据库密码 |
| `FORESIGHT_S3_ACCESS_KEY_ID` | `media.backend = "s3"` 时 | 对象存储的 access key |
| `FORESIGHT_S3_SECRET_ACCESS_KEY` | `media.backend = "s3"` 时 | 对象存储的 secret |
| `FORESIGHT_CONFIG` | 可选 | 配置文件路径，默认 `config.toml` |
| `FORESIGHT_API_HOST` / `FORESIGHT_API_PORT` | 可选 | 覆盖 `[api].host` / `[api].port` |

compose 另外用一个 `FORESIGHT_PORT` 决定宿主机上映射出来的端口（默认 `8000`），它只影响 compose，不影响服务本身。
