# MCP 通知功能

`send_notification` 通过用户已有通知设置中启用的 Webhook 发送消息。
复用现有发送器的格式适配、签名、超时和失败重试，不新增渠道、专用表或调度任务。
飞书、钉钉、企微和 Slack 文本包含 `[级别] 标题` 与正文；通用 Webhook 保留结构化字段。

## 配置与调用

- 在已有通知设置中保存 Webhook 地址，并在 `default_channels` 中启用 `webhook`。
- 使用原有 Agent `N` 权限；需要行情查询时使用 `R,N`。`N` 同时允许已有告警任务管理。
- hybrid OAuth 与其他通知写工具一致，使用 `quantdinger.write`，同时保留 HTTP 层的
  `quantdinger.read` 要求。专用 OAuth Agent Token 配置 `R,N`，凭据隔离规则不变。
- 不需要新增通知表或执行通知专用迁移；已有 Agent 基础表继续由原机制管理。

MCP 示例（仅文档示例，不实际发送）：

```json
{"title":"行情观察","message":"预设条件已出现，请人工核对。","level":"warning","idempotency_key":"observation-2026-10-13-001"}
```

`idempotency_key` 必填，1–120 字符。MCP 转换为 HTTP `Idempotency-Key` 请求头；
直接调用 `POST /api/agent/v1/notifications/send` 时将键放在请求头，JSON 只包含
`title`、`message`、可选 `level`。标题 1–200 字符，正文 1–8000 字符，级别为
`info/warning/critical`，拒绝空白文本和额外的目标地址、凭据、请求头等字段。

## 结果、幂等与失败处理

成功返回 `delivered`，表示 Webhook 接受，不表示用户已读；推送失败返回 `failed`。
重复请求复用现有 Agent 幂等机制：按 Token、方法、路径及键重放原响应，包含原失败结果，
不另返回 `duplicate`。同键不同正文或请求正在执行时返回 409。限流沿用现有 Agent 配置，
不增加每用户 10 次/分钟或跨 Token 24 小时去重机制。

Webhook 超时和 429/5xx 重试遵循原发送器；不新增补发机制。结果未知时应复用原键，
避免更换键导致重复推送。现有 Agent 幂等机制可接管过期的执行中请求，故不承诺严格只送达一次。
通知正文和凭据不进入 Agent 审计摘要，返回安全诊断。

## 验证与部署边界

业务代码集中在 `app/services/agent_notifications.py`；HTTP 接口位于独立的
`app/routes/agent_v1/notification_send.py`；MCP 工具位于
`mcp_server/src/quantdinger_mcp/notifications.py`。共享入口仅负责注册，
正文格式使用已有 `display.plain`，不向原通知格式化器添加专用事件分支。
保留共享审计和日志的必要脱敏修改，不复制认证、限流、幂等或发送实现。

后端测试：`python -m pytest tests/test_agent_notifications.py -q`。
MCP 测试：在根目录执行 `python -m pytest mcp_server/tests -q`。
测试模拟推送，不能用其结果替代实际渠道验收。

线上手改 OAuth 文件一致性、ChatGPT 实际调用及定时任务无人值守调用仍需单独验证；
现有 NEAR/USDT 监控保持不变。本次未部署、未发送真实通知。
