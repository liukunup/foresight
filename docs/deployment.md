# 部署

只有一份 `docker-compose.yml`。`.env` 里的环境变量同时决定启动哪些服务和应用程序使用哪些后端，不需要改 `config.toml` 就能切换架构。

| `COMPOSE_PROFILES` | 启动的服务 | `.env` 中的后端变量 |
|---|---|---|
| 留空 | SQLite + 本地附件 | `FORESIGHT_DATABASE_BACKEND=sqlite`、`FORESIGHT_MEDIA_BACKEND=local` |
| `mysql` | MySQL + 本地附件 | `FORESIGHT_DATABASE_BACKEND=mysql`、`FORESIGHT_MEDIA_BACKEND=local` |
| `postgresql` | PostgreSQL + 本地附件 | `FORESIGHT_DATABASE_BACKEND=postgresql` |
| `rustfs` | SQLite + RustFS | `FORESIGHT_MEDIA_BACKEND=s3` |
| `mysql,rustfs` | MySQL + RustFS | `FORESIGHT_DATABASE_BACKEND=mysql`、`FORESIGHT_MEDIA_BACKEND=s3` |

```bash
cp config.example.toml config.toml   # 填入 channel_ids，选好 backend
cp .env.example .env                 # 填入 token 和 COMPOSE_PROFILES

docker compose up -d
docker compose logs -f foresight
```

## 数据、端口与密钥

- 启用数据库 / RustFS 时，ForeSight 会等服务通过健康检查后再启动；数据在命名卷 `mysql-data` / `postgres-data` / `rustfs-data` 里，`down` 不会删掉。`./data` 只放 SQLite 文件、本地附件和失败批次文件，仍由 `init-data` 容器先交给 uid `10001`；换成 S3 媒体后端时可以不管这个目录。
- 非空的 `FORESIGHT_*` 环境变量覆盖 `config.toml` 中同名设置；连接信息可用 `FORESIGHT_DATABASE_HOST` / `_PORT` / `_NAME` / `_USER` / `_PATH`，S3 可用 `FORESIGHT_S3_BUCKET` / `_ENDPOINT_URL` / `_REGION` / `_PREFIX` / `_USE_PATH_STYLE`。密码仍只从 `.env` 的 `FORESIGHT_DB_PASSWORD` 读取。`FORESIGHT_API_TOKEN` 也必须设置，因为 API 绑定 `0.0.0.0`。
- 启用 `rustfs` 时 `s3-init` 会先建好 `foresight` 桶，不用手动去控制台创建。`FORESIGHT_S3_ACCESS_KEY_ID` / `FORESIGHT_S3_SECRET_ACCESS_KEY` 同时是 RustFS 根账号和 ForeSight 签名用的密钥；access key 只用大写字母和数字，secret 用 `openssl rand -base64 24` 生成，不要用默认的 `rustfsadmin`。
- 端口：`FORESIGHT_PORT` 控制宿主机 API 端口（默认 `8000`），`RUSTFS_PORT` / `RUSTFS_CONSOLE_PORT` 控制 RustFS 的 S3 API 和控制台（默认 `9000` / `9001`）。停止：`docker compose down`。
- 用 MiniMax 这类 OpenAI 兼容接口时，在 `.env` 里设 `FORESIGHT_LLM_API_KEY`，并在 `config.toml` 里写：

  ```toml
  [ai]
  enabled = true
  base_url = "https://api.minimax.chat/v1"
  model = "MiniMax-M3.1-Flash"
  ```

## 连接已有的外部数据库

要连已有的外部数据库，就只用默认 compose（不启用数据库 profile），并在 `config.toml` 的 `[database]` 段填 `backend` 和连接信息，或用 `.env` 里的 `FORESIGHT_DATABASE_*` 变量覆盖。密码只从 `FORESIGHT_DB_PASSWORD` 读取，不要写进 `config.toml`。
