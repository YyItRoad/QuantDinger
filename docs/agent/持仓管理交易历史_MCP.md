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
- `ext` 为通用 JSON 对象，当前通过 `ext.params.fixed_stop_price` 返回固定止损价；`schema_version: 1`，`snapshot_source: strategy_run` 表示来自匹配运行的参数快照。该值是运行配置，不代表交易所原生止损委托价格或实际平仓价。没有可靠来源时返回空对象，不补猜测值。

## 维护边界

共用查询仍在 `position_management_history.py`；Gateway 路由与 MCP 注册分别位于同名独立模块。通用快照的构建与显式补录收敛在 `position_management_history_ext.py`，不改变只读工具权限，也不在读取时补写数据库。

## 扩展快照与旧记录补录（2026-10-21）

新增现有归档表的 `ext JSONB NOT NULL DEFAULT '{}'` 列，不新建表、不单独增加固定止损价列。新旧库分别通过 `init.sql` 和幂等迁移 `20261021_position_management_history_ext.sql` 获得该列；升级迁移不自动补录数据。

先按现有 migration 服务流程完成 schema 更新，再在已配置目标数据库的后端环境预览：

```bash
PYTHONPATH=. python scripts/backfill_position_management_history_ext.py --user-id 7 --limit 1000
```

核对目标数据库、用户和预览统计后应用同一批：

```bash
PYTHONPATH=. python scripts/backfill_position_management_history_ext.py --user-id 7 --limit 1000 --apply
```

每批输出 `scanned/eligible/updated/skipped/last_id`。仅当 `scanned` 达到批次大小时，使用 `--after-id <last_id>` 检查下一批。`--user-id` 必填，单批最多 10000 条；支持重复执行。补录只匹配历史记录的运行编号、用户、策略、账户和市场，不使用当前策略配置替代历史；运行快照已删除、价格缺失或无效时保留未记录。

已有 `ext` 内容和已经记录的固定止损价不被覆盖；并发变化通过乐观比较保护。脚本只输出计数，不打印配置、账户密钥或历史内容。快照只抽取支持的公开配置项，不复制整个 runtime 配置；未来配置通过此构建入口扩展，无需每次修改数据库列。

前端详情使用归档中的固定止损价，缺失时显示“未记录”。部署需要同时发布对应后端和前端版本；MCP 原有两项工具复用响应，客户端工具说明更新需刷新发现。

本次扩展验证：后端聚焦回归 97 项通过，MCP 全套 65 项通过；前端相关测试 15 项通过，ESLint 与生产构建通过。补录查询和乐观更新使用隔离 SQLite 数据进行用户/策略/账户/市场匹配、幂等和并发保护验证；尚未在真实 PostgreSQL 执行迁移或修复生产记录。额外部署回归中的策略编译测试受本机沙箱内存限制失败，未修改或放宽沙箱保护。

机器契约见 [Agent OpenAPI](agent-openapi.json)。部署后须刷新客户端工具发现；本地测试通过不代表远端已经上线。

## 本次验证（2026-10-21）

- 历史服务、Gateway 与契约对齐测试：48 项通过，包含真实 SQL 执行的用户/账户隔离、分页排序、参数绑定、UTC 转换和 MCP → HTTP Gateway 联测。
- MCP 全套测试：65 项通过，使用仓库约束的 `mcp==1.28.1`，覆盖实际注册、只读注解、OAuth 权限分类、参数校验与 HTTP 转发。
- `git diff --check` 通过。测试环境有第三方 MCP / pydantic-settings 的 `lifespan` 类型定义警告，未影响上述执行结果。
- 未连接生产数据库验收、未推送、未部署；远端工具发现需发布后另行验证。
