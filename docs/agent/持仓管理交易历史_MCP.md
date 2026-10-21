# 持仓管理交易历史 MCP

## 使用

先调用 `list_trading_accounts()`，返回账户的 `id` 即 `credential_id`。以下工具均为只读，需要 Agent R 权限；OAuth 使用 `quantdinger.read`。

```python
list_position_management_trade_history(credential_id=2, page=1, page_size=20)
list_position_management_trade_history(
    credential_id=2, symbol="BTC/USDT", position_side="long",
    start_time="2026-10-01T00:00:00+08:00", end_time="2026-11-01T00:00:00+08:00",
)
get_position_management_trade_history(credential_id=2, history_id=123)
```

列表可选参数：`symbol` 精确匹配 `symbol_canonical`，`market_type` 为 `spot/swap/futures`，`position_side` 为 `long/short`，`source_strategy_id` 为原策略编号。时间必须携带时区，筛选 `exit_time`，包含开始、不包含结束。

列表返回 `items/total/page/page_size`，默认每页 20 条，最多 100 条。按退出时间倒序、历史编号倒序排列。并发新增归档可能导致跨页位移，不提供批量导出的快照一致性保证。详情同时要求账户与历史编号；不存在、跨账户及跨用户记录统一返回 404。非法参数返回 400。

## 数据口径

- 仅读取持仓管理的独立归档表。当前归档入口要求策略已停止、无剩余仓位；空结果不表示交易所从未发生交易。
- 每条记录对应原始成交快照，不等于一笔完整往返交易。
- `entry_time` 沿用策略创建时间，不保证是实际开仓成交时间；返回时间使用 UTC。
- `profit/profit_rate` 沿用现有收益算法；`profit_rate` 为百分数，涉及现有杠杆口径，不额外计算净收益。
- 手续费、资金费与币种分别返回。必须结合 `fee_status` 判断完整性，缺失或待确认不表示实际为零。
- 开平仓原因随归档返回，只读。工具不统计、不修改原因、不触发同步、不调用交易所。
- 原策略或账户已删除仍可凭历史账户编号读取本人归档；现有账户列表仅发现尚未删除的账户。

## 维护边界

共用查询仍在 `position_management_history.py`；新 Gateway 路由与 MCP 注册分别位于同名独立模块。现有 Human API 的默认查询与响应保持兼容。无需新增表、数据回写或迁移。

机器契约见 [Agent OpenAPI](agent-openapi.json)。部署后须刷新客户端工具发现；本地测试通过不代表远端已经上线。

## 本次验证（2026-10-21）

- 历史服务、Gateway 与契约对齐测试：48 项通过，包含真实 SQL 执行的用户/账户隔离、分页排序、参数绑定、UTC 转换和 MCP → HTTP Gateway 联测。
- MCP 全套测试：65 项通过，使用仓库约束的 `mcp==1.28.1`，覆盖实际注册、只读注解、OAuth 权限分类、参数校验与 HTTP 转发。
- `git diff --check` 通过。测试环境有第三方 MCP / pydantic-settings 的 `lifespan` 类型定义警告，未影响上述执行结果。
- 未连接生产数据库验收、未推送、未部署；远端工具发现需发布后另行验证。
