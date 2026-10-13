"""Send notifications through the existing configured Webhook sender."""
from __future__ import annotations

from app.services.signal_notifier import SignalNotifier
from app.services.user_preferences import get_notification_settings

MAX_TITLE = 200
MAX_MESSAGE = 8000


def validate_notification(body):
    if not isinstance(body, dict) or set(body) - {"title", "message", "level"}:
        raise ValueError("Only title, message and level are accepted")
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
    return out


def _webhook_payload(payload):
    """Use the existing display text contract for vendor Webhook adapters."""
    return {
        "event": "agent.notification",
        **{key: payload[key] for key in ("title", "message", "level")},
        "display": {"plain": f"[{payload['level'].upper()}] {payload['title']}\n{payload['message']}"},
    }


def send_notification(user_id, payload):
    """Never expose provider diagnostics or accept caller-selected destinations."""
    try:
        settings = get_notification_settings(user_id) or {}
        if "webhook" not in (settings.get("default_channels") or []) or not settings.get("webhook_url"):
            return {"status": "failed", "diagnostic": "notification_not_configured"}, 409
    except Exception:
        return {"status": "failed", "diagnostic": "notification_storage_unavailable"}, 503
    try:
        notifier = SignalNotifier()
        ok, _ = notifier._notify_webhook(
            url=settings["webhook_url"],
            payload=_webhook_payload(payload),
            token_override=settings.get("webhook_token"),
            signing_secret_override=settings.get("webhook_signing_secret"),
        )
    except Exception:
        ok = False
    status = "delivered" if ok else "failed"
    return {"status": status, "diagnostic": "webhook_accepted" if ok else "webhook_delivery_failed_or_unknown"}, 200 if ok else 502
