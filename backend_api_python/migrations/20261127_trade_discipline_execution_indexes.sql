-- 接管与退出沿用现有策略和快捷交易记录，唯一键防止重复创建和重复发送。
CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_discipline_adoption
    ON qd_strategies_trading (user_id, (trading_config->'position_management'->>'discipline_adoption_key'))
    WHERE trading_config->'position_management'->>'discipline_adoption_key' IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_quick_trade_discipline_exit
    ON qd_quick_trades(user_id, credential_id, (raw_result->'_discipline_exit'->>'key'))
    WHERE source = 'trade_discipline' AND (raw_result->'_discipline_exit'->>'key') IS NOT NULL;
