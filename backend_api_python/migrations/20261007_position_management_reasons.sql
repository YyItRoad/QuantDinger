-- 持仓管理沿用现有成交历史，仅增加两个个人原因字段。
ALTER TABLE qd_position_management_trade_history ADD COLUMN IF NOT EXISTS entry_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE qd_position_management_trade_history ADD COLUMN IF NOT EXISTS exit_reason TEXT NOT NULL DEFAULT '';
