# 部署

默认 compose 用 SQLite，数据库就是 `./data` 里的一个文件（容器内 `/app/data`）。端口默认 `8000`；想换就设环境变量 `FORESIGHT_PORT=8080 docker compose up -d`。停止用 `docker compose down`，数据在宿主机的 `./data` 里，`down` 不会删掉它。仓库还附带了两份把数据库也一起编排进来的 compose 文件：

| 数据库 | compose 文件 | 配置模板 |
|---|---|---|
| PostgreSQL | `docker-compose.postgresql.yml` | `config.postgresql.example.toml` |
| MySQL | `docker-compose.mysql.yml` | `config.mysql.example.toml` |

## 使用 MySQL / PostgreSQL 部署

以 PostgreSQL 为例：

```bash
cp config.postgresql.example.toml config.toml   # 填入 channel_ids；host/port/user/database 已对应 compose
cp .env.example .env                            # 填入 DISCORD_TOKEN、FORESIGHT_API_TOKEN、FORESIGHT_DB_PASSWORD
docker compose -f docker-compose.postgresql.yml up -d
docker compose -f docker-compose.postgresql.yml logs -f foresight
```

MySQL 把上面的 `postgresql` 换成 `mysql` 即可：用 `config.mysql.example.toml` 和 `docker-compose.mysql.yml`。

- 数据库跑在同一个 compose 里，ForeSight 会等数据库通过健康检查后再启动；数据库数据在命名卷 `postgres-data` / `mysql-data` 里，`down` 不会删掉它。要连已有的外部数据库，就只用默认 compose 并在 `config.toml` 的 `[database]` 里填 `backend` 和连接信息。
- 连接信息（backend/host/port/database/user）写在 `config.toml` 的 `[database]` 段；**密码只从 `.env` 的 `FORESIGHT_DB_PASSWORD` 读取**，不要写进 config.toml。`FORESIGHT_API_TOKEN` 同样必须设置，因为 API 绑定 `0.0.0.0`。
- `./data` 仍然放本地媒体和失败批次文件，仍由 `init-data` 容器先交给 uid `10001`；换成 S3 媒体后端时可以不管这个目录。
- 改宿主机端口仍用 `FORESIGHT_PORT`；停止用 `docker compose -f docker-compose.postgresql.yml down`（MySQL 换成对应文件）。
