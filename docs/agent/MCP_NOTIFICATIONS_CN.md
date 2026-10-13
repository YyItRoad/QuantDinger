# MCP 仅发送通知功能

`send_notification` 使用用户现有通知设置中已启用的 Webhook，复用原有发送器、
供应商格式适配、签名和重试。当前不选择其他通知渠道，不新增 AI 服务或交易操作。

## 权限与配置

- 在已有通知设置中保存 Webhook 地址，并在 `default_channels` 中启用 `webhook`。
- Agent Token 增加独立权限 `NS`；需要行情查询时使用 `R,NS`。
  原有 `N` 仍是告警任务管理权限，与 `NS` 不互相包含。
- hybrid OAuth 使用 `quantdinger.notify`，同时保留现有 HTTP 层要求的
  `quantdinger.read`。专用 `QUANTDINGER_MCP_OAUTH_AGENT_TOKEN` 配置 `R,NS`，
  不与主 Agent Token 或静态 MCP Token 混用。静态 MCP 调用同样需要后端 Token 的 `NS`。
- 更新后端前在 `backend_api_python` 中以 `QD_PROCESS_ROLE=migration` 执行
  `python -m app.commands.migrate`，应用 `20261013_agent_notifications.sql`。
  现有 Token 不会被自动扩权。

调用示例（仅文档示例，不实际发送）：

```json
{"title":"行情观察","message":"预设条件已出现，请人工核对证据。","level":"warning","idempotency_key":"observation-2026-10-13-001"}
```

标题 1–200 字符，正文 1–8000 字符，级别为 `info/warning/critical`，可选幂等键
1–120 字符。拒绝空白文本和调用者传入的目标地址、Token、请求头等额外字段。
返回 `delivered/duplicate/failed` 和安全诊断。403 表示缺少权限，429 表示限流，
409 表示配置缺失或键冲突，502/503 表示推送或存储失败。MCP 将 HTTP 失败呈现为工具错误。
日志、Agent 审计摘要和返回诊断不包含通知正文、Webhook 地址或密钥。

## 去重和限流

PostgreSQL 按用户协调多个 Token 和进程，每用户滚动一分钟最多 10 次新推送尝试，
同时保留既有 Agent 限流。相同键保留 24 小时，相同键不同正文返回 409。
只保存内容摘要及状态，过期记录在该用户下次尝试时清理。不传键则每次调用都算新通知。

`duplicate` 只表示已有发送预留，不保证送达，包括正在发送、失败或结果未知的情况。
超时或进程异常不释放预留；不要换新键重试结果未知的推送。原发送器对 429/5xx
可能重试一次，因此不承诺外部 Webhook 严格只送达一次。`delivered` 表示 Webhook
接受，不表示用户已读。每次网络尝试超时为 10 秒。

## 本地测试与上线前核对

安装后端开发依赖及 `pip install -e './mcp_server[dev]'` 后，从根目录运行
`python -m pytest mcp_server/tests -q`；在后端目录运行
`python -m pytest tests/test_agent_notifications.py -q`。
设置 `TEST_NOTIFICATION_DATABASE_URL` 指向本地测试 PostgreSQL 后，再运行
`python -m pytest tests/test_agent_notifications_postgres.py -q`。
数据库测试使用独立 schema，所有推送均模拟，不连接生产通知渠道。

实际服务源码位于 `mcp_server/src/quantdinger_mcp/`，Docker 构建复制到 `/app/src/`，
个人部署 Compose 指向 `trade.yangyang.fun`。本次基于仓库版本开发，用户将在本地
继续验证；尚未独立核验线上手改 OAuth 文件的一致性。基线 SHA-256 和英文详情见
[MCP 通知文档](MCP_NOTIFICATIONS.md)。部署前应比对线上文件和本次差异。
保留已有 JWT 校验、用户约束、Token 路由和 HTTP read 权限要求，仅增加发送权限并删除敏感调试日志。

尚未验证 ChatGPT 交互连接及定时任务能否无人值守调用 MCP；原 NEAR/USDT 定时监控不变。
