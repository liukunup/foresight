# 命令

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

`foresight stats` 的 `--since` / `--until` 与 API 的时间边界同义：纯日期表示整天，写法不对会直接报错退出（退出码 2）。`foresight analyze --since` 和 `foresight backfill --since` 用的是同一套规则，写法不对同样报错退出，不会静悄悄地什么都没做。时间边界规则见 [API](api.md#时间边界)。
