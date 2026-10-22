# MCP 整仓接管

复用持仓管理页面：同步真实仓位、选择策略源码、创建管理实例、登记整仓并提交启动命令。接管工具自身不提交新开仓订单，不按订单拆分仓位；启动后的交易行为由所选策略源码决定。

## 权限

- `list_managed_positions` 使用 R；结合 `get_account_snapshot` 查询实时仓位与管理归属。
- `adopt_position_management` 使用 W；外部 OAuth 对应 `quantdinger.write`。
- W 在这里授权整仓接管和自动启动，后续策略可能执行真实退出，不能将其当作只保存停止实例的工具。
- 接管不要求 T，不扣减快捷开仓额度，也不使用快捷下单的 `paper_only` 或 `AGENT_LIVE_TRADING_ENABLED` 开关。新开仓仍遵循原 T 及实盘限制。
- 账户所有权、市场和品种白名单、速率、幂等及审计仍生效。按交易所标识解析的实际仓位也必须通过白名单校验。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `credential_id` | 是 | 当前用户的账户 ID |
| `symbol` | 是 | 交易对，建议使用快照的规范名称 |
| `market_type` | 是 | `spot` 或 `swap` |
| `side` | 是 | `long` 或 `short` |
| `inst_id` | 否 | 快照返回的交易所标识 |
| `source_id` | 是 | 当前用户可用的已保存策略源码 ID |
| `name` | 是 | 管理实例名称 |
| `initial_capital` | 是 | 正数，沿用现有策略校验 |
| `timeframe` | 否 | 沿用源码及管理周期覆盖规则 |
| `params` | 按源码 | 固定止损放在 `params.fixed_stop_price` |
| `entry_reason` | 否 | 开仓或持有原因，最多 500 字 |
| `direction_mode` | 按源码 | 策略方向 |
| `account_risk` | 按源码 | 现有账户风险配置 |
| `notification_channels` | 否 | 通知渠道字符串数组 |
| `notification_targets` | 否 | 现有通知目标对象 |
| `confirm_adoption` | 是 | 明确授权整仓接管并启动后传 `true` |
| `idempotency_key` | 是 | 稳定幂等键，最多 120 字符 |

数量、价格、杠杆由后端重新读取整仓数据，不接受覆盖。身份来自令牌，接管模式沿用现有实盘流程，不接受 `user_id`、`qty`、`execution_mode` 等字段。

以下仅展示调用结构，实际账户、源码、参数和金额须先核实：

```json
{
  "credential_id": 2,
  "symbol": "BTC/USDT",
  "market_type": "swap",
  "side": "long",
  "source_id": 9,
  "name": "BTC 整仓管理",
  "initial_capital": 100,
  "direction_mode": "long_only",
  "timeframe": "4h",
  "params": {"fixed_stop_price": 60000},
  "entry_reason": "管理现有整笔仓位",
  "confirm_adoption": true,
  "idempotency_key": "manage-account2-btc-long-request1"
}
```

## HTTP 与结果

- `GET /api/agent/v1/trading/accounts/{credential_id}/managed-positions` 返回管理归属 `data.items`，不是交易所实时快照。
- `POST /api/agent/v1/trading/accounts/{credential_id}/managed-strategies` 使用同组业务参数，但账户在路径、幂等键在 `Idempotency-Key` 请求头，不在 JSON 中重复传入。
- HTTP 201 与 `code: 0` 表示创建完成。保留现有返回的策略 ID、整仓数量、价格、杠杆、`status` 和可选 `warning`。
- `starting` 仅表示已提交启动命令，不证明 Worker 已运行。启动失败时保留已创建实例、`stopped` 和警告，必须如实报告。

## 边界

既有人工仓位包含在整仓范围内；已有策略归属的仓位拒绝重复接管。部分成交已有真实仓位可以接管当前整仓，不自动撤销剩余挂单，也不承诺后续成交增量自动归属。

同一方法、路径、请求体重试复用幂等键，正常重放返回原结果。状态不明先查询归属和原实例，不可用新键盲目重建。开仓成功但接管失败，只恢复接管，不重开仓或擅自平仓。

不额外创建或撤销原生保护单。策略退出与归档保持原流程，固定止损继续通过历史 ext 快照展示。工具不提供开仓与接管的原子事务，也不提供聊天断开后的自动编排。
