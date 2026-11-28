"""纪律通知的站内事实与有限投递任务，复用现有表、个人设置和发送器。"""

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

from app.services.notifications.email_templates import render_detail_email, strategy_messages
from app.services.portfolio_monitor_notifications import resolve_notification_delivery
from app.services.signal_notifier import SignalNotifier
from app.services.user_preferences import get_notification_settings, VALID_NOTIFICATION_CHANNELS
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)
_META = "_discipline_notice"


def _delivery_result(result):
    """只保存固定分类与安全状态码，提供方原文可能包含密钥或私人信息，必须丢弃。"""
    ok = bool(result.get("ok")) and not result.get("skipped")
    skipped = bool(result.get("skipped"))
    saved = {"ok": ok, "skipped": skipped,
             "diagnostic": "channel_disabled" if skipped else "accepted" if ok else "delivery_failed_or_unknown"}
    if ok or skipped:
        return saved
    if result.get("diagnostic") == "target_not_configured":
        return dict(saved, diagnostic="target_not_configured")
    error = str(result.get("error") or "")
    code = re.match(r"^(http_[1-5]\d{2}|vendor_-?\d{1,12})(?=:|$)", error)
    if code:
        lower = error.casefold()
        # 提供方描述只用于归类，不写入数据库或日志；未知业务码也只保留数字。
        reason = "keyword_rejected" if "keyword" in lower or "关键词" in lower else (
            "signature_rejected" if "signature" in lower or "签名" in lower else "provider_rejected")
        return dict(saved, diagnostic=reason, provider_code=code.group(1))
    for prefix, reason in (("timeout", "timeout"), ("connection_error", "connection_failed"),
                           ("missing_", "provider_configuration_missing"),
                           ("webhook_private_address_blocked", "target_security_rejected"),
                           ("webhook_host_unresolvable", "target_dns_failed")):
        if error.startswith(prefix):
            return dict(saved, diagnostic=reason)
    return saved


def _payload(row):
    return json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]


def _update(cur, user_id, notice_id, payload):
    cur.execute("UPDATE qd_strategy_notifications SET payload_json=%s WHERE id=%s AND user_id=%s",
                (json.dumps(payload, ensure_ascii=False, default=str), notice_id, user_id))


def record_notice(*, user_id, event_key, kind, symbol="", strategy_id=None, **facts):
    """先落站内事实，再交异步任务发送；事件重入只返回原记录，不再创建消息。"""
    try:
        facts.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
        facts = json.loads(json.dumps(facts, default=lambda value:
            format(value, "f") if isinstance(value, Decimal) else
            value.isoformat() if isinstance(value, datetime) else str(value)))
        settings = get_notification_settings(user_id) or {}
        selected = settings.get("default_channels") or []
        selected = selected if isinstance(selected, list) else []
        channels = list(dict.fromkeys(["browser", *(ch.strip().lower() for ch in selected
            if isinstance(ch, str) and ch.strip().lower() in VALID_NOTIFICATION_CHANNELS)]))
        cfg = resolve_notification_delivery(user_id, {"channels": channels})
        messages = strategy_messages(cfg.get("language"))["discipline"]
        title = messages["events"][kind]
        fields = dict(symbol=symbol, strategy_id=strategy_id, **facts)
        if facts.get("credential_id"):
            # 通知只读取本用户的账户名称，不读取或缓存交易所凭证内容。
            with get_db_connection() as db:
                cur = db.cursor()
                try:
                    cur.execute("SELECT name FROM qd_exchange_credentials WHERE id=%s AND user_id=%s",
                                (facts["credential_id"], user_id))
                    account = cur.fetchone()
                finally:
                    cur.close()
            if account and account.get("name"):
                fields["credential_id"] = f"{account['name']} (#{facts['credential_id']})"
        if fields.get("recovered") is True:
            fields["recovered"] = messages["recovered_value"]
        if fields.get("side") in messages["sides"]:
            fields["side"] = messages["sides"][fields["side"]]
        if fields.get("reason") in messages["reasons"]:
            fields["reason"] = messages["reasons"][fields["reason"]]
        rows = [(label, fields[key]) for key, label in messages["fields"].items()
                if fields.get(key) is not None and str(fields[key]) != ""]
        message = "\n".join(f"{label}: {value}" for label, value in rows)
        payload = {"event": "qd.trade_discipline", "version": 1, "event_id": event_key,
                   "title": title, "message": message, "symbol": symbol,
                   "details": rows,
                   "strategy_id": strategy_id, "timestamp": int(time.time()), **facts,
                   "display": {"plain": f"{title}\n{message}"},
                   _META: {"key": event_key, "kind": kind, "language": cfg.get("language"),
                           "channels": [ch for ch in channels if ch != "browser"],
                           "delivered": [], "skipped": [], "attempts": 0, "published_at": 0, "next_at": 0}}
        # 原表没有业务唯一键，沿用 PostgreSQL 事务锁保护“检查后创建”。锁内不发网络请求。
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                            (f"discipline-notice:{user_id}:{event_key}",))
                query = """SELECT id FROM qd_strategy_notifications WHERE user_id=%s
                    AND NULLIF(payload_json,'')::jsonb->'_discipline_notice'->>'key'=%s LIMIT 1"""
                cur.execute(query, (user_id, event_key))
                row = cur.fetchone()
                # 升级前已经有站内异常提示时补投原记录的外部渠道，不再新建一条提示。
                legacy_field = {"exit_unconfirmed": "request_id", "start_failed": "failure_key"}.get(kind)
                if not row and legacy_field and facts.get(legacy_field) is not None:
                    cur.execute("""SELECT id FROM qd_strategy_notifications WHERE user_id=%s
                        AND strategy_id IS NOT DISTINCT FROM %s AND signal_type=%s
                        AND NULLIF(payload_json,'')::jsonb->>%s=%s LIMIT 1""",
                                (user_id, strategy_id, f"discipline_{kind}", legacy_field, str(facts[legacy_field])))
                    row = cur.fetchone()
                    if row:
                        _update(cur, user_id, row["id"], payload)
                if not row:
                    ok, _ = SignalNotifier()._notify_browser(
                        user_id=user_id, strategy_id=strategy_id, symbol=symbol,
                        signal_type=f"discipline_{kind}", channels=channels,
                        title=title, message=message, payload=payload,
                    )
                    if not ok:
                        return None
                    cur.execute(query, (user_id, event_key))
                    row = cur.fetchone()
                db.commit()
                return int(row["id"]) if row else None
            finally:
                cur.close()
    except Exception:
        # 失败只影响通知，不能让调用方重新下单或重建接管实例。
        logger.exception("trade_discipline notice_record_failed user_id=%s kind=%s", user_id, kind)
        return None


def deliver_notice(user_id, notice_id):
    """由现有 Celery 执行。成功渠道持久化，失败最多尝试三次，不保存目标和密钥。"""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                        (f"discipline-delivery:{user_id}:{notice_id}",))
            if not cur.fetchone()["locked"]:
                return {"status": "busy"}
            cur.execute("SELECT id,symbol,signal_type,payload_json FROM qd_strategy_notifications WHERE id=%s AND user_id=%s",
                        (notice_id, user_id))
            row = cur.fetchone()
            if not row:
                return {"status": "missing"}
            payload = _payload(row)
            meta = payload.get(_META)
            if meta and all(ch in meta["delivered"] + meta.get("skipped", []) for ch in meta["channels"]):
                return {"status": "skipped" if meta.get("skipped") else "delivered", "notice_id": notice_id}
            if meta and meta["attempts"] >= 3:
                return {"status": "failed", "notice_id": notice_id}
            if not meta or meta["next_at"] > time.time():
                return {"status": "deferred"}
            settings = get_notification_settings(user_id) or {}
            enabled = settings.get("default_channels") or []
            pending = [ch for ch in meta["channels"] if ch not in meta["delivered"] + meta.get("skipped", [])]
            cfg = resolve_notification_delivery(user_id, {"channels": pending, "language": meta["language"]})
            # 目标在投递时重新解析，尊重用户取消渠道；不在通知 JSON 中缓存鉴权信息。
            results = {}
            for channel in pending:
                if channel not in enabled:
                    results[channel] = {"ok": True, "skipped": True}
                    continue
                # 没有个人目标时不能回落到发送器的全局运维目标，避免跨用户泄露通知。
                if not (cfg.get("targets") or {}).get(channel):
                    results[channel] = {"ok": False, "diagnostic": "target_not_configured"}
                    continue
                results.update(SignalNotifier()._deliver_channels(
                    strategy_id=None, symbol=row["symbol"], signal_type=row["signal_type"],
                    cfg=cfg, channels=[channel], payload={k: v for k, v in payload.items() if k != _META},
                    rendered={"title": payload["title"], "plain": payload["message"],
                              "email_html": render_detail_email(payload["title"],
                                  payload.get("details") or [("", payload["message"])], language=meta["language"])},
                ))
            meta["attempts"] += 1
            meta["delivered"] = list(dict.fromkeys([*meta["delivered"],
                *(ch for ch, result in results.items() if result.get("ok") and not result.get("skipped"))]))
            meta["skipped"] = list(dict.fromkeys([*meta.get("skipped", []),
                *(ch for ch, result in results.items() if result.get("skipped"))]))
            meta["results"] = {**meta.get("results", {}), **{ch: _delivery_result(result) for ch, result in results.items()}}
            meta["next_at"] = int(time.time()) + 60
            _update(cur, user_id, notice_id, payload)
            db.commit()
            pending = [ch for ch in meta["channels"] if ch not in meta["delivered"] + meta.get("skipped", [])]
            status = ("skipped" if meta.get("skipped") else "delivered") if not pending else "failed" if meta["attempts"] >= 3 else "pending"
            logger.info("trade_discipline notice_delivery user_id=%s notice_id=%s status=%s failed_channels=%s errors=%s",
                        user_id, notice_id, status, pending,
                        {ch: meta["results"].get(ch, {}).get("provider_code") or
                         meta["results"].get(ch, {}).get("diagnostic") for ch in pending})
            return {"status": status, "notice_id": notice_id}
        finally:
            cur.close()


def publish_pending_notices(user_id, *, still_active):
    """Worker 只补入现有有限任务队列；PostgreSQL 仍是通知事实及逐渠道结果的来源。"""
    from app.celery_app import celery_app

    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute("""SELECT id,payload_json FROM qd_strategy_notifications WHERE user_id=%s
                AND NULLIF(payload_json,'')::jsonb->'_discipline_notice' IS NOT NULL
                AND NOT (((payload_json::jsonb->'_discipline_notice'->'delivered') ||
                         COALESCE(payload_json::jsonb->'_discipline_notice'->'skipped','[]'::jsonb)) @>
                         (payload_json::jsonb->'_discipline_notice'->'channels'))
                AND (payload_json::jsonb->'_discipline_notice'->>'attempts')::int < 3
                AND (payload_json::jsonb->'_discipline_notice'->>'next_at')::bigint <= EXTRACT(EPOCH FROM NOW())
                AND (payload_json::jsonb->'_discipline_notice'->>'published_at')::bigint <= EXTRACT(EPOCH FROM NOW())-60
                ORDER BY id LIMIT 10""", (user_id,))
            rows = cur.fetchall()
        finally:
            cur.close()
    published = 0
    for row in rows:
        if not still_active() or published >= 3:
            break
        meta = _payload(row)[_META]
        now = int(time.time())
        if (meta["attempts"] >= 3 or meta["next_at"] > now
                or all(ch in meta["delivered"] + meta.get("skipped", []) for ch in meta["channels"])):
            continue
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("""UPDATE qd_strategy_notifications SET payload_json=jsonb_set(payload_json::jsonb,
                    '{_discipline_notice,published_at}',to_jsonb(%s::bigint))::text
                    WHERE user_id=%s AND id=%s
                      AND COALESCE((payload_json::jsonb->'_discipline_notice'->>'published_at')::bigint,0)<=%s
                    RETURNING id""", (now, user_id, row["id"], now - 60))
                claimed = cur.fetchone()
                db.commit()
            finally:
                cur.close()
        if not claimed:
            continue
        try:
            # 队列不可用时限时退出，下一轮从原通知补投，不阻塞交易线程长期重连。
            with celery_app.connection_for_write(connect_timeout=1) as connection:
                celery_app.send_task("quantdinger.tasks.trade_discipline_notice", args=[user_id, row["id"]],
                                     connection=connection, retry=False, ignore_result=True)
            published += 1
        except Exception:
            logger.warning("trade_discipline notice_queue_unavailable user_id=%s notice_id=%s", user_id, row["id"])
            break
