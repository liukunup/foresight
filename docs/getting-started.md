# 准备 Discord 机器人与本地运行

## ⚠️ 必须开启 MESSAGE CONTENT INTENT

> [!WARNING]
> **没有开启 MESSAGE CONTENT INTENT，采集到的每条消息正文都是空的，而且不会有任何报错。**
>
> 这个坑非常安静：bot 照样连上 Discord、照样收到「有人发言了」的事件、照样把消息写进数据库——只是每条消息的 `content` 都是空字符串。日志一片正常，只有对着数据库看一眼才发现「有消息但没内容」。
>
> 开启路径：**Discord 开发者后台 → 你的 Application → Bot 页签 → 找到 `MESSAGE CONTENT INTENT` → 打开开关 → 保存**。
> 改完必须**重启 bot**（`docker compose restart foresight` 或重启进程）才生效。

## 准备 Discord 机器人

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications)，新建 Application
2. 在 **Bot** 页签点 **Reset Token**，复制这个 token —— 它就是 `DISCORD_TOKEN`
3. 打开 **Privileged Gateway Intents**，**必须开启 MESSAGE CONTENT INTENT**（不开的话正文永远是空的，而且不报错）
4. 在 **OAuth2 → URL Generator** 勾选 `bot` + `Read Message History` + `Send Messages`，用生成的链接邀请 bot 进服务器
5. 在 Discord 客户端右键目标频道 → 复制频道 ID（开发者模式：设置 → 高级 → 开发者模式），填进 `config.toml` 的 `[discord].channel_ids`

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

详细说明见 [配置](configuration.md) 和 [命令](cli.md)。

## 开发

```bash
uv run pytest -v
uv run foresight init-db --config config.example.toml
```
