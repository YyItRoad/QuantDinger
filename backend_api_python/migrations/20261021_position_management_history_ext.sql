-- Extensible managed-history snapshots; historical repair is explicitly invoked.
ALTER TABLE qd_position_management_trade_history
    ADD COLUMN IF NOT EXISTS ext JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(ext) = 'object');
