-- 独立分析模块：只保存任务与结果，本迁移不启动任何分析。
CREATE TABLE IF NOT EXISTS qd_market_state_tasks (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    market VARCHAR(120) NOT NULL,
    symbol VARCHAR(120) NOT NULL,
    exchange_id VARCHAR(120) NOT NULL DEFAULT '',
    market_type VARCHAR(16) NOT NULL CHECK (market_type IN ('spot', 'swap')),
    instrument_id VARCHAR(120) NOT NULL DEFAULT '',
    timeframe VARCHAR(8) NOT NULL CHECK (timeframe IN ('1h', '4h', '1d')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (id, user_id),
    CHECK (deleted_at IS NULL OR enabled = FALSE)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_market_state_active_task
ON qd_market_state_tasks(user_id, market, symbol, exchange_id, market_type, timeframe)
WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_market_state_tasks_user_created
ON qd_market_state_tasks(user_id, created_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS qd_market_state_results (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    task_id BIGINT NOT NULL,
    -- 不允许删除任务级联删除结果，同时保证结果与任务同属一个用户。
    FOREIGN KEY (task_id, user_id) REFERENCES qd_market_state_tasks(id, user_id),
    market VARCHAR(120) NOT NULL,
    symbol VARCHAR(120) NOT NULL,
    exchange_id VARCHAR(120) NOT NULL DEFAULT '',
    market_type VARCHAR(16) NOT NULL,
    instrument_id VARCHAR(120) NOT NULL DEFAULT '',
    timeframe VARCHAR(8) NOT NULL,
    bar_close_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trend VARCHAR(40) NOT NULL,
    structure VARCHAR(40) NOT NULL,
    ma_state VARCHAR(40) NOT NULL,
    position VARCHAR(40) NOT NULL,
    momentum VARCHAR(40) NOT NULL,
    phase VARCHAR(32) NOT NULL CHECK (phase IN ('BASE', 'TRANSITION_UP', 'ADVANCE', 'TOP', 'TRANSITION_DOWN', 'DECLINE')),
    confidence SMALLINT NOT NULL CHECK (confidence BETWEEN 1 AND 5),
    summary TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    UNIQUE (task_id, bar_close_at)
);
CREATE INDEX IF NOT EXISTS idx_market_state_results_user_created
ON qd_market_state_results(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_market_state_results_filter
ON qd_market_state_results(user_id, symbol, timeframe, created_at DESC, id DESC);
