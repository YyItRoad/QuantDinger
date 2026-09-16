-- 持仓管理策略交易历史。
-- 该表保存删除策略前的独立快照，不对策略、订单、运行实例或交易所账户建立外键。
CREATE TABLE IF NOT EXISTS qd_position_management_trade_history (
    id BIGSERIAL PRIMARY KEY,
    source_trade_id BIGINT NOT NULL,
    source_strategy_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,

    strategy_name VARCHAR(255) NOT NULL DEFAULT '',
    strategy_type VARCHAR(50) NOT NULL DEFAULT '',
    market_category VARCHAR(50) NOT NULL DEFAULT '',
    execution_mode VARCHAR(20) NOT NULL DEFAULT '',
    strategy_status VARCHAR(20) NOT NULL DEFAULT '',
    timeframe VARCHAR(16) NOT NULL DEFAULT '',
    leverage DECIMAL(20, 8) NOT NULL DEFAULT 1,
    entry_time TIMESTAMP,
    strategy_run_id BIGINT NOT NULL DEFAULT 0,
    strategy_started_at TIMESTAMP,
    strategy_stopped_at TIMESTAMP,
    strategy_stop_reason TEXT NOT NULL DEFAULT '',

    exchange_id VARCHAR(50) NOT NULL DEFAULT '',
    credential_id INTEGER NOT NULL DEFAULT 0,
    exchange_account_name VARCHAR(100) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT '',
    inst_id VARCHAR(80) NOT NULL DEFAULT '',
    settlement_ccy VARCHAR(20) NOT NULL DEFAULT '',

    pending_order_id BIGINT NOT NULL DEFAULT 0,
    order_intent_id BIGINT NOT NULL DEFAULT 0,
    order_type VARCHAR(24) NOT NULL DEFAULT '',
    order_status VARCHAR(24) NOT NULL DEFAULT '',
    requested_amount DECIMAL(28, 12) NOT NULL DEFAULT 0,
    filled_amount DECIMAL(28, 12) NOT NULL DEFAULT 0,
    exchange_order_id VARCHAR(160) NOT NULL DEFAULT '',
    client_order_id VARCHAR(100) NOT NULL DEFAULT '',
    signal_at TIMESTAMP,
    order_created_at TIMESTAMP,
    order_sent_at TIMESTAMP,
    exit_time TIMESTAMP,

    symbol VARCHAR(80) NOT NULL DEFAULT '',
    symbol_canonical VARCHAR(80) NOT NULL DEFAULT '',
    trade_type VARCHAR(40) NOT NULL DEFAULT '',
    position_side VARCHAR(12) NOT NULL DEFAULT '',
    entry_price DECIMAL(28, 12) NOT NULL DEFAULT 0,
    exit_price DECIMAL(28, 12) NOT NULL DEFAULT 0,
    trade_value DECIMAL(28, 12) NOT NULL DEFAULT 0,
    profit_rate DECIMAL(20, 8) NOT NULL DEFAULT 0,
    profit DECIMAL(28, 12) NOT NULL DEFAULT 0,
    profit_ccy VARCHAR(20) NOT NULL DEFAULT '',
    close_reason VARCHAR(255) NOT NULL DEFAULT '',
    trade_recorded_at TIMESTAMP,

    fill_source VARCHAR(32) NOT NULL DEFAULT '',
    grid_order_id BIGINT NOT NULL DEFAULT 0,
    grid_matched_profit DECIMAL(28, 12),
    execution_event_id BIGINT NOT NULL DEFAULT 0,
    exchange_fill_id VARCHAR(160) NOT NULL DEFAULT '',

    commission DECIMAL(28, 12) NOT NULL DEFAULT 0,
    commission_ccy VARCHAR(20) NOT NULL DEFAULT '',
    commission_quote DECIMAL(28, 12),
    fee_status VARCHAR(24) NOT NULL DEFAULT '',
    fee_source VARCHAR(24) NOT NULL DEFAULT '',
    funding_fee_total DECIMAL(28, 12) NOT NULL DEFAULT 0,
    funding_fee_ccy VARCHAR(20) NOT NULL DEFAULT '',

    archived_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (source_trade_id)
);

CREATE INDEX IF NOT EXISTS idx_pm_trade_history_user_exit
ON qd_position_management_trade_history(user_id, exit_time DESC);

CREATE INDEX IF NOT EXISTS idx_pm_trade_history_strategy
ON qd_position_management_trade_history(source_strategy_id);

CREATE INDEX IF NOT EXISTS idx_pm_trade_history_exchange_symbol
ON qd_position_management_trade_history(user_id, exchange_id, symbol, exit_time DESC);

CREATE INDEX IF NOT EXISTS idx_pm_trade_history_account_exit
ON qd_position_management_trade_history(user_id, credential_id, exit_time DESC);
