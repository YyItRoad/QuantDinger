# 持仓管理渐进式重构计划

## 一、目标

在不改变页面接口、数据库表、策略源码格式和交易执行语义的前提下，将持仓管理专用代码逐步从官方高频更新文件中收口。每一步独立提交、独立测试、独立审查，不把代码搬迁与业务修复混在同一个提交中。

## 二、必须保持的现有行为

1. 仓位页面继续使用 `GET /api/account/managed-positions` 查询管理状态。
2. 继续使用 `POST /api/account/managed-strategies` 创建持仓管理实例。
3. 接管时仍调用原 `StrategyService` 创建标准 Strategy API V2 实盘实例。
4. 整笔手工仓位仍登记到 `qd_strategy_positions`。
5. 实例继续绑定真实品种、方向、数量、开仓价、杠杆和运行周期。
6. 运行、日志、风控判断和退出订单继续由原 `TradingExecutor` 执行。
7. 策略自身成交平仓后继续立即检查并停止管理实例。
8. 普通策略不进入持仓管理生命周期。

## 三、分步提交

### 提交 1：锁定现有行为

- 只调整和新增测试、文档，不改生产代码。
- 将持仓管理专用测试从官方通用测试文件迁入独立文件。
- 固定接管、部署、更新、立即停止和交易所仓位同步等现有行为。

### 提交 2：独立接口与查询

- 将两个持仓管理接口迁入独立路由文件。
- 将已管理仓位查询迁入持仓管理服务。
- API 地址、请求、响应和数据库查询语义保持不变。

### 提交 3：收口 Strategy V2 适配

- 将品种和周期解析集中到持仓管理适配器。
- 保持原部署结果、运行候选品种和执行周期不变。
- 不修改生命周期和订单路径。

### 提交 4：补充外部平仓

- 保留成交平仓后的立即停止检查。
- 在真实 L1 账户仓位同步后增加补充检查。
- 手工平仓、交易所止损或其他外部平仓在下一次成功同步后停止管理实例。
- 同步失败或普通策略不得触发停止。

### 提交 5：完整验收

- 运行持仓管理定向测试。
- 运行 Strategy API V2 和 Trading Worker 回归测试。
- 运行完整后端测试与质量基线。
- 对比 `upstream/main`，记录官方既有文件的最终修改范围。

## 四、审查原则

- 每个提交只包含一个主题。
- 每个提交通过对应测试后暂停，等待人工审查。
- 不自动部署、不自动推送。
- 不通过隐藏源码副本、动态改写策略源码或新增插件框架减少表面文件数量。
- 任何业务时机或数据语义变化必须放入单独的功能提交。

## 五、执行结果

完成日期：2026 年 9 月 1 日。

### 5.1 分步提交

1. `aec08c8 test: 锁定持仓管理现有行为`
2. `195390a refactor: 独立持仓管理接口与查询`
3. `a6d145c refactor: 收口持仓管理策略适配`
4. `4266d74 refactor: 收口持仓管理生命周期`

### 5.2 验证结果

- 完整后端测试：1695 项通过，5 项按测试条件跳过，6 个子测试通过，零失败。
- Ruff 静态检查：`app`、`scripts`、`tests` 全部通过。
- 持仓管理定向测试覆盖创建实例、真实品种和周期覆盖、成交平仓、交易所侧平仓、普通策略隔离以及持久化停止命令。
- 测试在一次性容器中执行，未替换或重启现有服务，未连接实盘交易流程。

### 5.3 相对官方上游的最终边界

以下三个原项目文件已恢复为官方上游内容，不再包含持仓管理代码：

- `app/routes/strategy_account_routes.py`
- `app/services/live_trading/account_positions.py`
- `app/services/strategy_lifecycle.py`

持仓管理业务集中在以下三个专用文件：

- `app/routes/strategy_position_management_routes.py`
- `app/services/live_trading/position_management.py`
- `app/services/strategy_v2/position_management_timeframe.py`

原项目生产文件只保留无法取消的薄接入点：

- `app/routes/strategy.py`：注册持仓管理接口。
- `app/services/live_trading/account_snapshot.py`：为现货仓位补充最新价格。
- `app/services/live_trading/records.py`：策略自身成交平仓后通知生命周期检查。
- `app/services/live_trading/strategy_position_sync.py`：交易所仓位同步后通知生命周期检查。
- `app/services/strategy.py`：更新策略实例时保留持仓管理配置。
- `app/services/strategy_v2/deployment.py`：调用部署适配并保存实例配置。
- `app/services/strategy_v2/runtime.py`：允许实盘会话使用已经完成周期覆盖的编译结果。
- `app/services/trading_executor.py`：调用真实品种和周期适配器，后续行情、策略和订单流程保持原样。

上述八个原项目生产文件相对官方上游合计增加 65 行、删除 12 行，净增加 53 行；原 Trading Worker 文件没有修改。
