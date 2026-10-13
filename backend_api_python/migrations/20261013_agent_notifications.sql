-- Send-only notifications: store hashes and execution state, never message bodies or secrets.
CREATE TABLE IF NOT EXISTS qd_agent_notification_sends (
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    key_hash VARCHAR(64) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('started', 'delivered', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, key_hash)
);
CREATE INDEX IF NOT EXISTS idx_agent_notification_rate ON qd_agent_notification_sends(user_id, created_at);
