-- Recording-only plan aggregates. No execution tables are modified.
CREATE TABLE IF NOT EXISTS qd_trade_plans (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    current_version_id UUID,
    lifecycle TEXT NOT NULL DEFAULT 'draft' CHECK (lifecycle IN ('draft','observing','cancelled','expired','completed')),
    row_revision BIGINT NOT NULL DEFAULT 1 CHECK (row_revision > 0),
    parent_plan_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, id),
    FOREIGN KEY (user_id, parent_plan_id) REFERENCES qd_trade_plans(user_id, id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS qd_trade_plan_versions (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    schema_version INTEGER NOT NULL DEFAULT 1,
    snapshot JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('web','mcp','system')),
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_id, version_no),
    UNIQUE (user_id, plan_id, id),
    FOREIGN KEY (user_id, plan_id) REFERENCES qd_trade_plans(user_id, id)
        DEFERRABLE INITIALLY DEFERRED
);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_trade_plan_current_version' AND conrelid='qd_trade_plans'::regclass) THEN
        ALTER TABLE qd_trade_plans ADD CONSTRAINT fk_trade_plan_current_version
        FOREIGN KEY (user_id,id,current_version_id) REFERENCES qd_trade_plan_versions(user_id,plan_id,id)
        DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS qd_trade_plan_events (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL,
    plan_version_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL CHECK (source IN ('web','mcp','system')),
    actor_id TEXT NOT NULL,
    FOREIGN KEY (user_id,plan_id,plan_version_id) REFERENCES qd_trade_plan_versions(user_id,plan_id,id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS qd_trade_plan_requests (
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    request_id VARCHAR(120) NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_plans_user_updated ON qd_trade_plans(user_id,updated_at DESC,id);
CREATE INDEX IF NOT EXISTS idx_trade_plan_events_time ON qd_trade_plan_events(user_id,plan_id,recorded_at,id);
CREATE INDEX IF NOT EXISTS idx_trade_plan_signal_version
    ON qd_trade_plan_events(user_id,plan_id,plan_version_id,recorded_at DESC,id DESC)
    WHERE event_type='signal_assessment';
CREATE OR REPLACE FUNCTION qd_trade_plan_immutable() RETURNS TRIGGER AS $$
BEGIN
    -- Preserve the platform's authorized user deletion, without a mutable bypass flag.
    -- Ordinary history changes still fail while the owning user exists.
    IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM qd_users WHERE id=OLD.user_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Trade plan history is append only' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_trade_plan_versions_immutable ON qd_trade_plan_versions;
CREATE TRIGGER trg_trade_plan_versions_immutable BEFORE UPDATE OR DELETE ON qd_trade_plan_versions
FOR EACH ROW EXECUTE FUNCTION qd_trade_plan_immutable();
DROP TRIGGER IF EXISTS trg_trade_plan_events_immutable ON qd_trade_plan_events;
CREATE TRIGGER trg_trade_plan_events_immutable BEFORE UPDATE OR DELETE ON qd_trade_plan_events
FOR EACH ROW EXECUTE FUNCTION qd_trade_plan_immutable();
