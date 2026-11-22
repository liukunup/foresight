# ForeSight

Discord 频道消息转储服务：只要用户在受监控的频道发言，消息就会连同编辑、删除、表情回应和附件一起落库，并通过 HTTP API、Prometheus 指标和一个可安装的 Agent Skill 供人和 AI 查询分析。

## 特性

- **实时采集**：消息、编辑、删除、表情回应全部捕获（编辑只保留最新内容，删除是软删除）
- **附件归档**：图片/视频/音频下载到本地或任意 S3 兼容存储（RustFS、MinIO、R2、OSS）
- **AI 分析**：一次模型调用同时产出翻译、情绪、问题提取
- **查询 API**：资源端点 + 统计端点 + 只读 SQL 端点，全部只读连接
- **Agent Skill**：装上之后 AI 就能自己查、自己分析、自己出结论
- **Prometheus 指标**：采集、写入队列、AI、媒体、API 全链路可观测
- **三种数据库**：SQLite（默认）/ PostgreSQL / MySQL
- **历史补录**：一条命令把过去 N 天的消息导入

## ⚠️ 开始之前：必须开启 MESSAGE CONTENT INTENT

> [!WARNING]
> **没有开启 MESSAGE CONTENT INTENT，采集到的每条消息正文都是空的，而且不会有任何报错。**
>
> 这个坑非常安静：bot 照样连上 Discord、照样收到「有人发言了」的事件、照样把消息写进数据库——只是每条消息的 `content` 都是空字符串。日志一片正常，只有对着数据库看一眼才发现「有消息但没内容」。
>
> 开启路径：**Discord 开发者后台 → 你的 Application → Bot 页签 → 找到 `MESSAGE CONTENT INTENT` → 打开开关 → 保存**。
> 改完必须**重启 bot**（`docker compose restart foresight` 或重启进程）才生效。

## 快速开始（Docker，推荐）

只有一份 `docker-compose.yml`。`.env` 里的环境变量同时决定启动哪些服务和应用程序使用哪些后端，不需要改 `config.toml` 就能切换架构。

| `COMPOSE_PROFILES` | 启动的服务 | `.env` 中的后端变量 |
|---|---|---|
| 留空 | SQLite + 本地附件 | `FORESIGHT_DATABASE_BACKEND=sqlite`、`FORESIGHT_MEDIA_BACKEND=local` |
| `mysql` | MySQL + 本地附件 | `FORESIGHT_DATABASE_BACKEND=mysql`、`FORESIGHT_MEDIA_BACKEND=local` |
| `postgresql` | PostgreSQL + 本地附件 | `FORESIGHT_DATABASE_BACKEND=postgresql` |
| `rustfs` | SQLite + RustFS | `FORESIGHT_MEDIA_BACKEND=s3` |
| `mysql,rustfs` | MySQL + RustFS | `FORESIGHT_DATABASE_BACKEND=mysql`、`FORESIGHT_MEDIA_BACKEND=s3` |

```bash
git clone https://github.com/liukunup/foresight.git
cd foresight

cp config.example.toml config.toml   # 填入 channel_ids，选好 backend
cp .env.example .env                 # 填入 token 和 COMPOSE_PROFILES

docker compose up -d
docker compose logs -f foresight
```

服务会在 <http://127.0.0.1:8000> 提供 API，接口文档在 <http://127.0.0.1:8000/docs>。

- 启用数据库 / RustFS 时，ForeSight 会等服务通过健康检查后再启动；数据在命名卷 `mysql-data` / `postgres-data` / `rustfs-data` 里，`down` 不会删掉。`./data` 只放 SQLite 文件、本地附件和失败批次文件。
- 非空的 `FORESIGHT_*` 环境变量覆盖 `config.toml` 中同名设置；连接信息可用 `FORESIGHT_DATABASE_HOST` / `_PORT` / `_NAME` / `_USER` / `_PATH`，S3 可用 `FORESIGHT_S3_BUCKET` / `_ENDPOINT_URL` / `_REGION` / `_PREFIX` / `_USE_PATH_STYLE`。密码仍只从 `.env` 的 `FORESIGHT_DB_PASSWORD` 读取。`FORESIGHT_API_TOKEN` 也必须设置，因为 API 绑定 `0.0.0.0`。
- 启用 `rustfs` 时 `rustfs-bucket` 会先建好 `foresight` 桶，不用手动去控制台创建。`FORESIGHT_S3_ACCESS_KEY_ID` / `FORESIGHT_S3_SECRET_ACCESS_KEY` 同时是 RustFS 根账号和 ForeSight 签名用的密钥；access key 只用大写字母和数字，secret 用 `openssl rand -base64 24` 生成，不要用默认的 `rustfsadmin`。
- 端口：`FORESIGHT_PORT` 控制宿主机 API 端口（默认 `8000`），`RUSTFS_PORT` / `RUSTFS_CONSOLE_PORT` 控制 RustFS 的 S3 API 和控制台（默认 `9000` / `9001`）。停止：`docker compose down`。
- 用 MiniMax 这类 OpenAI 兼容接口时，在 `.env` 里设 `FORESIGHT_LLM_API_KEY`，并在 `config.toml` 里写：

  ```toml
  [ai]
  enabled = true
  base_url = "https://api.minimax.chat/v1"
  model = "MiniMax-M3.1-Flash"
  ```

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
3. 打开 **Privileged Gateway Intents**，**必须开启 MESSAGE CONTENT INTENT**（不开的话正文永远是空的，而且不报错）
4. 在 **OAuth2 → URL Generator** 勾选 `bot` + `Read Message History` + `Send Messages`，用生成的链接邀请 bot 进服务器
5. 在 Discord 客户端右键目标频道 → 复制频道 ID（开发者模式：设置 → 高级 → 开发者模式），填进 `config.toml` 的 `[discord].channel_ids`

## 配置

`config.toml` 完整示例见 [`config.example.toml`](config.example.toml)。

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
| `COMPOSE_PROFILES` | Docker 部署 | 逗号分隔的可选服务：`mysql` / `postgresql` / `rustfs`；留空为 SQLite + 本地附件 |
| `FORESIGHT_DATABASE_BACKEND`、`FORESIGHT_MEDIA_BACKEND` | 可选 | 应用使用的数据库 / 媒体后端；非空时覆盖 `config.toml` |
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

## 命令

| 命令 | 作用 |
|---|---|
| `foresight init-db` | 建表 |
| `foresight run` | 只跑采集器 |
| `foresight serve` | 只跑 HTTP API（`--host` / `--port` 可覆盖） |
| `foresight both` | 单进程同时跑采集器和 API（compose 默认） |
| `foresight backfill --days 7` | 导入最近 7 天的历史消息（不补表情、不跑 AI） |
| `foresight analyze` | 分析还没分析过的消息（需要 `[ai].enabled = true`） |
| `foresight query --sql "SELECT ..." --db data/foresight.db` | 只读查询 |
| `foresight stats --db data/foresight.db` | 概览统计 |

除 `query` 和 `stats` 外，每条命令都接受 `--config` 指定别的配置文件 —— 这两条命令直接读数据库文件，用 `--db` 指定路径。

`serve` 用的是**只读连接**，它不会建表。单独跑 API 之前必须先建一次表：

```bash
uv run foresight init-db     # 或 docker run ... foresight init-db
uv run foresight serve
```

`run` 和 `both` 自己会建表，不用手动执行。

`foresight stats` 的 `--since` / `--until` 与 API 的时间边界同义：纯日期表示整天，写法不对会直接报错退出（退出码 2）。`foresight analyze --since` 和 `foresight backfill --since` 用的是同一套规则，写法不对同样报错退出，不会静悄悄地什么都没做。

## 安装 AI Skill

```bash
./scripts/install_skill.sh --target opencode     # 或 --target claude
export FORESIGHT_API_TOKEN="..."                 # 脚本会提示下一步
```

脚本把 skill 复制到 agent 的 skills 目录，并把连接信息写在 `~/.config/foresight/connection.toml`（里面只有 API 地址和**环境变量名**，不存 token 明文）。

装好之后 AI 就能自己查这个归档：「上周谁发言最多」「大家都在抱怨什么」「这个频道的活跃时间是什么时候」——它会自己去调 API、自己算、自己给结论，而不是让你手动翻数据库。

## API

设置了 `FORESIGHT_API_TOKEN` 时，`/api/*` 下的所有请求都需要 `Authorization: Bearer <FORESIGHT_API_TOKEN>`；不设置时（这时只允许绑定本机地址）API 接受匿名请求。`/metrics` 和 `/healthz` 不在 `/api` 下，不需要 Bearer token —— `/metrics` 可以用 `api.metrics_token` 单独再加一道令牌。

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 存活探针，不需要 token，不返回任何关于数据的内容 |
| `GET /api/health` | 状态与计数 |
| `GET /api/messages` | 消息列表，支持频道/作者/时间/关键词过滤 |
| `GET /api/messages/{id}` | 单条消息（带 AI 分析结果） |
| `GET /api/users` | 发言排行 |
| `GET /api/users/{id}` | 单个用户 |
| `GET /api/channels` | 频道统计 |
| `GET /api/reactions` | 表情明细 |
| `GET /api/stats/overview` | 总量、活跃人数、删除率、编辑率 |
| `GET /api/stats/top-talkers` | 发言排行 |
| `GET /api/stats/activity` | 时间分布（`bucket=hour\|day`） |
| `GET /api/stats/topics` | 高频议题 |
| `POST /api/query` | 只读 SQL（单条 SELECT/WITH，写操作被数据库引擎拒绝） |
| `GET /metrics` | Prometheus 指标（不在 `/api` 下，不需要 token） |

分页信封：`{"total": N, "limit": 50, "offset": 0, "has_more": true, "items": []}`。

时间边界 `since` / `until` 两种写法都可以：

- 完整时间戳，如 `2026-11-01T10:00:00+00:00` —— 按字面比较，两端都是闭区间；日期和时间之间的分隔符写成空格（`2026-11-01 10:00:00`）也行，会被统一成 `T`；
- 纯日期，如 `2026-11-01` —— `since` 取这天 `00:00:00` 起点，`until` 取这天 `23:59:59.999999` 终点，也就是**整天都算在内**。

其他写法（比如 `since=last-tuesday`）会被拒绝，返回 422 并在报错里点名是哪个参数不对。

## Prometheus 指标

`GET /metrics` 暴露全部指标，例如 `foresight_messages_captured_total`、`foresight_writer_queue_depth`、`foresight_writer_dropped_total`、`foresight_api_requests_total`、`foresight_db_messages_total`。

`foresight_writer_dropped_total` 按 `kind` 和 `reason`（`queue_full` / `stopped`）统计被写队列丢掉的写入操作 —— 队列满了或者写队列已经停了的时候，操作不会被写进数据库，这个计数让丢失看得见。

> 指标标签不会使用 `user_id`（基数太高会打爆 Prometheus），用户维度请查 API。

## 常见问题

**正文是空的 / 抓不到消息**
Discord 开发者后台的 **MESSAGE CONTENT INTENT** 没开。在 Bot 页面打开后重启 bot。这是唯一一个「看起来一切正常但数据是空的」的坑。

**`SQLite is locked`**
服务在写库时又有人查。SQLite 已启用 WAL 和 5 秒 busy timeout；长时间锁通常是有别的进程在写同一个文件，确认没有多个容器共享同一个库文件。

**`FORESIGHT_API_TOKEN` 必须设置**
API 绑定到 `0.0.0.0`（非本机）时必须设置 token，否则服务拒绝启动。Docker compose 就是绑定 `0.0.0.0`，所以 `.env` 里的 `FORESIGHT_API_TOKEN` 必须填。

**容器起来但写不了数据库（permission denied）**
容器内的服务用 uid `10001` 运行，数据目录必须归它所有。`docker compose up` 会先跑一个 `init-data` 容器把 `./data` 交给这个用户，你不用手动处理；手动 `docker run` 时请自己建好目录并 `chown 10001:10001 data`。

**容器反复重启（restart loop）**
先看 `docker compose logs foresight`，原因都在日志里。常见两种：Discord 登录失败（token 写错、过期或被吊销）时进程会退出，容器按 `restart: unless-stopped` 反复重启 —— 改好 `DISCORD_TOKEN` 后 `docker compose restart foresight`；端口被占用时换一个 `FORESIGHT_API_PORT`。

**Discord 断线了，API 还在吗**
在。`both` 里采集器和 API 是分开看的：采集器掉线（网络故障、Discord 挂掉）时它会等一会儿再连，API 这段时间照常服务，日志里会写明采集器停了。只有登录失败是致命的 —— token 错了重试也没用，这时进程会退出。

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
