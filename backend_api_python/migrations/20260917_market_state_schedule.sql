-- 调度内部元数据；默认不开启执行，不改变用户启停状态。
ALTER TABLE qd_market_state_tasks ADD COLUMN IF NOT EXISTS lease_token UUID;
ALTER TABLE qd_market_state_tasks ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE qd_market_state_tasks ADD COLUMN IF NOT EXISTS last_attempt_bar TIMESTAMPTZ;
