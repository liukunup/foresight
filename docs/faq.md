# 常见问题

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
