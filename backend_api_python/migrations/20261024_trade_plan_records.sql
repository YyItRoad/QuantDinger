-- Research records only. Trading tables are read, never mutated.
CREATE TABLE IF NOT EXISTS qd_trade_plan_attempts (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL,
    plan_version_id UUID NOT NULL,
    attempt_no INTEGER NOT NULL,
    account_snapshot JSONB NOT NULL,
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('live','paper')),
    risk_snapshot JSONB NOT NULL,
    note TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    UNIQUE (user_id,plan_id,id),
    UNIQUE (plan_id,attempt_no),
    FOREIGN KEY (user_id,plan_id,plan_version_id) REFERENCES qd_trade_plan_versions(user_id,plan_id,id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS qd_trade_plan_links (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL,
    attempt_id UUID NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('strategy','order','quick_order','trade','history')),
    source_id BIGINT NOT NULL,
    source_key TEXT NOT NULL,
    trade_id BIGINT,
    execution_event_id BIGINT,
    fill_identity TEXT,
    snapshot JSONB NOT NULL,
    attribution TEXT NOT NULL CHECK (attribution IN ('confirmed','pending')),
    reason TEXT NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (user_id,plan_id,attempt_id) REFERENCES qd_trade_plan_attempts(user_id,plan_id,id)
        DEFERRABLE INITIALLY DEFERRED
);
DROP INDEX IF EXISTS uq_plan_link_source;
CREATE UNIQUE INDEX uq_plan_link_source ON qd_trade_plan_links(user_id,source_key)
    WHERE NOT revoked AND source_kind IN ('trade','history');
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_link_attempt_source ON qd_trade_plan_links(user_id,attempt_id,source_key)
    WHERE NOT revoked;
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_link_trade ON qd_trade_plan_links(user_id,trade_id) WHERE NOT revoked AND trade_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_link_event ON qd_trade_plan_links(user_id,execution_event_id) WHERE NOT revoked AND execution_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_link_fill ON qd_trade_plan_links(user_id,fill_identity) WHERE NOT revoked AND fill_identity IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_plan_links_attempt ON qd_trade_plan_links(user_id,attempt_id,recorded_at);
CREATE TABLE IF NOT EXISTS qd_trade_plan_reviews (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL,
    attempt_id UUID NOT NULL,
    review_no INTEGER NOT NULL,
    assessment JSONB NOT NULL,
    evidence_snapshot JSONB NOT NULL,
    change_reason TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    UNIQUE(attempt_id,review_no),
    FOREIGN KEY (user_id,plan_id,attempt_id) REFERENCES qd_trade_plan_attempts(user_id,plan_id,id)
        DEFERRABLE INITIALLY DEFERRED
);
DROP TRIGGER IF EXISTS trg_plan_attempts_immutable ON qd_trade_plan_attempts;
CREATE TRIGGER trg_plan_attempts_immutable BEFORE UPDATE OR DELETE ON qd_trade_plan_attempts
FOR EACH ROW EXECUTE FUNCTION qd_trade_plan_immutable();
DROP TRIGGER IF EXISTS trg_plan_reviews_immutable ON qd_trade_plan_reviews;
CREATE TRIGGER trg_plan_reviews_immutable BEFORE UPDATE OR DELETE ON qd_trade_plan_reviews
FOR EACH ROW EXECUTE FUNCTION qd_trade_plan_immutable();
CREATE OR REPLACE FUNCTION qd_trade_plan_link_immutable() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' AND NOT EXISTS(SELECT 1 FROM qd_users WHERE id=OLD.user_id) THEN RETURN OLD; END IF;
    IF TG_OP = 'UPDATE' AND OLD.revoked = FALSE AND NEW.revoked = TRUE
       AND (to_jsonb(OLD) - 'revoked') = (to_jsonb(NEW) - 'revoked') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'Trade plan link evidence is immutable' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_plan_links_immutable ON qd_trade_plan_links;
CREATE TRIGGER trg_plan_links_immutable BEFORE UPDATE OR DELETE ON qd_trade_plan_links
FOR EACH ROW EXECUTE FUNCTION qd_trade_plan_link_immutable();
