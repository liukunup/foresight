# API

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

## 时间边界

时间边界 `since` / `until` 两种写法都可以：

- 完整时间戳，如 `2026-11-01T10:00:00+00:00` —— 按字面比较，两端都是闭区间；日期和时间之间的分隔符写成空格（`2026-11-01 10:00:00`）也行，会被统一成 `T`；
- 纯日期，如 `2026-11-01` —— `since` 取这天 `00:00:00` 起点，`until` 取这天 `23:59:59.999999` 终点，也就是**整天都算在内**。

其他写法（比如 `since=last-tuesday`）会被拒绝，返回 422 并在报错里点名是哪个参数不对。

指标详情见 [Prometheus 指标](observability.md)。
