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
> 没开启 MESSAGE CONTENT INTENT，采集到的每条消息正文都是空的，而且不会有任何报错。
> 在 Discord 开发者后台 → Bot 页签打开该开关并保存，然后**重启 bot** 才生效。详见 [准备 Discord 机器人](docs/getting-started.md#准备-discord-机器人)。

## 快速开始（Docker，推荐）

先按 [准备 Discord 机器人](docs/getting-started.md#准备-discord-机器人) 拿到 bot token 和频道 ID。

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

服务会在 <http://127.0.0.1:8000> 提供 API，接口文档在 <http://127.0.0.1:8000/docs>。启用数据库 / RustFS 时，ForeSight 会等服务通过健康检查后再启动；`./data` 只放 SQLite 文件、本地附件和失败批次文件，停止用 `docker compose down`（数据不会被删掉）。更多部署细节见 [部署](docs/deployment.md)。

## 文档

- [入门（Discord 机器人、本地运行、开发）](docs/getting-started.md)
- [部署（MySQL / PostgreSQL / RustFS）](docs/deployment.md)
- [配置](docs/configuration.md)
- [命令](docs/cli.md)
- [安装 AI Skill](docs/agent-skill.md)
- [API](docs/api.md)
- [Prometheus 指标](docs/observability.md)
- [常见问题](docs/faq.md)

## 许可

见 [LICENSE](LICENSE)。
