"""Bounded, user-scoped sends through the existing configured Webhook sender."""
from __future__ import annotations

import hashlib
import json
import uuid

from app.services.signal_notifier import SignalNotifier
from app.services.user_preferences import get_notification_settings
from app.utils.db import get_db_connection

MAX_TITLE = 200
MAX_MESSAGE = 8000
MAX_KEY = 120
RATE_PER_MINUTE = 10
TTL_SECONDS = 86400


def validate_notification(body):
    if not isinstance(body, dict) or set(body) - {"title", "message", "level", "idempotency_key"}:
        raise ValueError("Only title, message, level and idempotency_key are accepted")
    out = {}
    for name, limit in (("title", MAX_TITLE), ("message", MAX_MESSAGE)):
        value = body.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must contain 1 to {limit} characters")
        out[name] = value.strip()
    level = body.get("level", "info")
    if not isinstance(level, str) or level not in {"info", "warning", "critical"}:
        raise ValueError("level must be info, warning or critical")
    out["level"] = level
    key = body.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY):
        raise ValueError("idempotency_key must contain 1 to 120 characters")
    out["idempotency_key"] = key.strip() if key is not None else None
    return out


def _reserve(user_id, payload):
    """Serialize reservations per user across tokens/processes, before network I/O."""
    key = payload["idempotency_key"] or str(uuid.uuid4())
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    body_hash = hashlib.sha256(json.dumps(
        {k: payload[k] for k in ("title", "message", "level")}, sort_keys=True,
    ).encode()).hexdigest()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (731902, int(user_id)))
        cur.execute("DELETE FROM qd_agent_notification_sends WHERE user_id = %s AND expires_at <= NOW()", (user_id,))
        cur.execute("SELECT request_hash, status FROM qd_agent_notification_sends WHERE user_id = %s AND key_hash = %s", (user_id, key_hash))
        existing = cur.fetchone()
        if existing:
            db.commit()
            cur.close()
            if existing["request_hash"] != body_hash:
                return "conflict", key_hash
            return "duplicate", key_hash
        cur.execute("SELECT COUNT(*) AS count FROM qd_agent_notification_sends WHERE user_id = %s AND created_at > NOW() - INTERVAL '1 minute'", (user_id,))
        if cur.fetchone()["count"] >= RATE_PER_MINUTE:
            db.commit()
            cur.close()
            return "rate_limited", key_hash
        cur.execute("""INSERT INTO qd_agent_notification_sends
            (user_id, key_hash, request_hash, status, expires_at)
            VALUES (%s, %s, %s, 'started', NOW() + (%s * INTERVAL '1 second'))""",
            (user_id, key_hash, body_hash, TTL_SECONDS))
        db.commit()
        cur.close()
    return "reserved", key_hash


def _finish(user_id, key_hash, status):
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("UPDATE qd_agent_notification_sends SET status = %s WHERE user_id = %s AND key_hash = %s", (status, user_id, key_hash))
        db.commit()
        cur.close()


def send_notification(user_id, payload):
    """Never expose provider diagnostics or accept caller-selected destinations."""
    try:
        settings = get_notification_settings(user_id) or {}
        if "webhook" not in (settings.get("default_channels") or []) or not settings.get("webhook_url"):
            return {"status": "failed", "diagnostic": "notification_not_configured"}, 409
        state, key_hash = _reserve(user_id, payload)
    except Exception:
        return {"status": "failed", "diagnostic": "notification_storage_unavailable"}, 503
    if state == "duplicate":
        return {"status": "duplicate", "diagnostic": "already_reserved_within_24_hours"}, 200
    if state != "reserved":
        return {"status": "failed", "diagnostic": state}, 429 if state == "rate_limited" else 409
    try:
        notifier = SignalNotifier()
        notifier.timeout_sec = 10
        ok, _ = notifier._notify_webhook(
            url=settings["webhook_url"],
            payload={"event": "agent.notification", **{k: payload[k] for k in ("title", "message", "level")}},
            token_override=settings.get("webhook_token"),
            signing_secret_override=settings.get("webhook_signing_secret"),
        )
    except Exception:
        ok = False
    status = "delivered" if ok else "failed"
    try:
        _finish(user_id, key_hash, status)
    except Exception:
        # Keep the committed reservation even if acknowledgement persistence fails.
        return {"status": "failed", "diagnostic": "delivery_state_unknown_do_not_retry_with_new_key"}, 503
    return {"status": status, "diagnostic": "webhook_accepted" if ok else "webhook_delivery_failed_or_unknown"}, 200 if ok else 502
