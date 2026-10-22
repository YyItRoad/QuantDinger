"""Persistent WebSocket order-entry transports for supported exchanges.

The transport deliberately distinguishes a connection failure before an order
frame is sent from an uncertain outcome after the frame is sent.  Only the
former may safely fall back to REST without risking a duplicate order.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import ssl
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

from app.services.live_trading.base import LiveTradingError, _get_requests_verify

logger = logging.getLogger(__name__)


class WebSocketOrderUnavailable(Exception):
    """The order frame was not sent, so REST fallback remains safe."""


class WebSocketOrderUnknown(LiveTradingError):
    """The order frame was sent but no authoritative response was received."""


def websocket_order_entry_enabled() -> bool:
    return str(os.environ.get("LIVE_TRADING_WS_ORDER_ENTRY", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _max_connections() -> int:
    try:
        value = int(os.environ.get("LIVE_TRADING_WS_ORDER_MAX_CONNECTIONS", "64") or 64)
    except (TypeError, ValueError):
        value = 64
    return min(256, max(4, value))


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _ssl_options() -> Dict[str, Any]:
    verify = _get_requests_verify()
    if verify is False:
        return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
    if isinstance(verify, str):
        return {"ca_certs": verify}
    return {}


def _proxy_options() -> Dict[str, Any]:
    proxy_url = str(os.environ.get("PROXY_URL", "") or "").strip()
    if not proxy_url:
        return {}
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return {}
    scheme = str(parsed.scheme or "http").lower()
    options: Dict[str, Any] = {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": scheme,
    }
    if parsed.username is not None:
        options["http_proxy_auth"] = (parsed.username, parsed.password or "")
    return options


class _PersistentRpcSocket:
    def __init__(
        self,
        *,
        venue: str = "unknown",
        url: str,
        headers: Optional[Dict[str, str]] = None,
        authenticate: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.venue = str(venue)
        self.url = str(url)
        self.headers = dict(headers or {})
        self.authenticate = authenticate
        self._socket: Any = None
        self._lock = threading.RLock()
        self._last_used = 0.0

    @property
    def last_used(self) -> float:
        return self._last_used

    def close(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def _connect(self) -> Any:
        sock = None
        try:
            import websocket
        except ImportError as exc:
            raise WebSocketOrderUnavailable("websocket-client dependency is missing") from exc

        connect_timeout = _float_env(
            "LIVE_TRADING_WS_ORDER_CONNECT_TIMEOUT_SEC", 2.5, 0.5, 15.0
        )
        try:
            sock = websocket.create_connection(
                self.url,
                timeout=connect_timeout,
                header=self.headers or None,
                sslopt=_ssl_options(),
                enable_multithread=True,
                **_proxy_options(),
            )
            sock.settimeout(
                _float_env("LIVE_TRADING_WS_ORDER_RESPONSE_TIMEOUT_SEC", 4.0, 0.5, 20.0)
            )
            if self.authenticate is not None:
                self.authenticate(sock)
            self._socket = sock
            return sock
        except WebSocketOrderUnavailable:
            raise
        except Exception as exc:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise WebSocketOrderUnavailable(f"WebSocket order connection unavailable: {exc}") from exc

    def request(
        self,
        payload: Dict[str, Any],
        *,
        matches: Callable[[Dict[str, Any]], bool],
        terminal: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            started_at = time.monotonic()
            reused = self._socket is not None
            idle_reconnect = _float_env(
                "LIVE_TRADING_WS_ORDER_IDLE_RECONNECT_SEC", 20.0, 5.0, 300.0
            )
            if (
                self._socket is not None
                and self._last_used > 0
                and (time.monotonic() - self._last_used) >= idle_reconnect
            ):
                self.close()
            sock = self._socket or self._connect()
            sent = False
            try:
                sent = True
                sock.send(_json_dumps(payload))
                deadline = time.monotonic() + _float_env(
                    "LIVE_TRADING_WS_ORDER_RESPONSE_TIMEOUT_SEC", 4.0, 0.5, 20.0
                )
                while time.monotonic() < deadline:
                    raw = sock.recv()
                    if raw in (None, ""):
                        raise ConnectionError("WebSocket order connection closed")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    message = json.loads(str(raw))
                    if not isinstance(message, dict) or not matches(message):
                        continue
                    if terminal is not None and not terminal(message):
                        continue
                    self._last_used = time.monotonic()
                    logger.info(
                        "WebSocket order response venue=%s elapsed_ms=%.1f connection=%s",
                        self.venue,
                        (self._last_used - started_at) * 1000,
                        "reused" if reused else "new",
                    )
                    return message
                raise TimeoutError("WebSocket order response timed out")
            except Exception as exc:
                self.close()
                if sent:
                    raise WebSocketOrderUnknown(
                        "WebSocket order execution status unknown; reconcile by client order id before retrying"
                    ) from exc
                raise WebSocketOrderUnavailable(f"WebSocket order unavailable: {exc}") from exc


_pool_lock = threading.RLock()
_pool: "OrderedDict[str, _PersistentRpcSocket]" = OrderedDict()


def _pool_key(venue: str, url: str, api_key: str, qualifier: str = "") -> str:
    digest = hashlib.sha256(f"{url}|{api_key}|{qualifier}".encode("utf-8")).hexdigest()
    return f"{venue}:{digest}"


def _connection(
    *,
    venue: str,
    url: str,
    api_key: str,
    qualifier: str = "",
    headers: Optional[Dict[str, str]] = None,
    authenticate: Optional[Callable[[Any], None]] = None,
) -> _PersistentRpcSocket:
    key = _pool_key(venue, url, api_key, qualifier)
    with _pool_lock:
        existing = _pool.get(key)
        if existing is not None:
            _pool.move_to_end(key)
            return existing
        conn = _PersistentRpcSocket(
            venue=venue,
            url=url,
            headers=headers,
            authenticate=authenticate,
        )
        _pool[key] = conn
        while len(_pool) > _max_connections():
            _, stale = _pool.popitem(last=False)
            stale.close()
        return conn


def close_websocket_order_pool() -> None:
    with _pool_lock:
        values = list(_pool.values())
        _pool.clear()
    for conn in values:
        conn.close()


def _binance_signature(secret_key: str, params: Dict[str, Any]) -> str:
    items = sorted((str(key), value) for key, value in params.items() if key != "signature")
    query = urlencode(items, doseq=True)
    return hmac.new(secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()


def submit_binance_order(
    *,
    api_key: str,
    secret_key: str,
    params: Dict[str, Any],
    futures: bool,
    demo: bool,
) -> Optional[Dict[str, Any]]:
    if not websocket_order_entry_enabled():
        return None
    if futures:
        url = (
            "wss://testnet.binancefuture.com/ws-fapi/v1"
            if demo
            else "wss://ws-fapi.binance.com/ws-fapi/v1"
        )
    else:
        url = (
            "wss://demo-ws-api.binance.com/ws-api/v3"
            if demo
            else "wss://ws-api.binance.com:443/ws-api/v3"
        )
    request_id = uuid.uuid4().hex
    signed = dict(params or {})
    signed["apiKey"] = api_key
    signed["signature"] = _binance_signature(secret_key, signed)
    conn = _connection(venue="binance-futures" if futures else "binance-spot", url=url, api_key=api_key)
    try:
        response = conn.request(
            {"id": request_id, "method": "order.place", "params": signed},
            matches=lambda msg: str(msg.get("id") or "") == request_id,
        )
    except WebSocketOrderUnavailable:
        return None
    status = int(response.get("status") or 0)
    if status < 200 or status >= 300:
        raise LiveTradingError(f"Binance WebSocket order rejected: {response}")
    result = response.get("result")
    return result if isinstance(result, dict) else {"raw": result}


def _okx_authenticator(api_key: str, secret_key: str, passphrase: str) -> Callable[[Any], None]:
    def authenticate(sock: Any) -> None:
        timestamp = str(time.time())
        prehash = f"{timestamp}GET/users/self/verify"
        signature = base64.b64encode(
            hmac.new(secret_key.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        sock.send(
            _json_dumps(
                {
                    "op": "login",
                    "args": [
                        {
                            "apiKey": api_key,
                            "passphrase": passphrase,
                            "timestamp": timestamp,
                            "sign": signature,
                        }
                    ],
                }
            )
        )
        reply = json.loads(str(sock.recv()))
        if not isinstance(reply, dict) or reply.get("event") != "login" or str(reply.get("code")) != "0":
            raise WebSocketOrderUnavailable("OKX WebSocket login failed")

    return authenticate


def submit_okx_order(
    *,
    api_key: str,
    secret_key: str,
    passphrase: str,
    body: Dict[str, Any],
    simulated: bool,
) -> Optional[Dict[str, Any]]:
    if not websocket_order_entry_enabled():
        return None
    url = (
        "wss://wspap.okx.com:8443/ws/v5/private"
        if simulated
        else "wss://ws.okx.com:8443/ws/v5/private"
    )
    request_id = uuid.uuid4().hex
    conn = _connection(
        venue="okx",
        url=url,
        api_key=api_key,
        qualifier="simulated" if simulated else "live",
        authenticate=_okx_authenticator(api_key, secret_key, passphrase),
    )
    try:
        response = conn.request(
            {"id": request_id, "op": "order", "args": [dict(body or {})]},
            matches=lambda msg: str(msg.get("id") or "") == request_id,
        )
    except WebSocketOrderUnavailable:
        return None
    data = response.get("data") if isinstance(response, dict) else None
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if str(response.get("code") or "") != "0" or str(first.get("sCode") or "0") != "0":
        raise LiveTradingError(f"OKX WebSocket order rejected: {response}")
    return {"code": "0", "msg": str(response.get("msg") or ""), "data": data or []}


def _bybit_authenticator(api_key: str, secret_key: str) -> Callable[[Any], None]:
    def authenticate(sock: Any) -> None:
        expires = int(time.time() * 1000) + 10_000
        signature = hmac.new(
            secret_key.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256
        ).hexdigest()
        sock.send(_json_dumps({"op": "auth", "args": [api_key, expires, signature]}))
        reply = json.loads(str(sock.recv()))
        ret_code = reply.get("retCode") if isinstance(reply, dict) else None
        if not isinstance(reply, dict) or reply.get("op") != "auth" or int(ret_code if ret_code is not None else -1) != 0:
            raise WebSocketOrderUnavailable("Bybit WebSocket login failed")

    return authenticate


def submit_bybit_order(
    *,
    api_key: str,
    secret_key: str,
    body: Dict[str, Any],
    base_url: str,
    recv_window_ms: int,
    broker_referer: str,
    timestamp_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if not websocket_order_entry_enabled():
        return None
    host = str(base_url or "").lower()
    if "demo" in host:
        return None
    url = (
        "wss://stream-testnet.bybit.com/v5/trade"
        if "testnet" in host
        else "wss://stream.bybit.com/v5/trade"
    )
    request_id = uuid.uuid4().hex
    header = {
        "X-BAPI-TIMESTAMP": str(int(timestamp_ms or time.time() * 1000)),
        "X-BAPI-RECV-WINDOW": str(int(recv_window_ms)),
    }
    if broker_referer:
        header["Referer"] = str(broker_referer)
    conn = _connection(
        venue="bybit",
        url=url,
        api_key=api_key,
        qualifier=broker_referer,
        authenticate=_bybit_authenticator(api_key, secret_key),
    )
    try:
        response = conn.request(
            {
                "reqId": request_id,
                "header": header,
                "op": "order.create",
                "args": [dict(body or {})],
            },
            matches=lambda msg: str(msg.get("reqId") or "") == request_id,
        )
    except WebSocketOrderUnavailable:
        return None
    ret_code = response.get("retCode")
    if int(ret_code if ret_code is not None else -1) != 0:
        raise LiveTradingError(f"Bybit WebSocket order rejected: {response}")
    return {
        "retCode": 0,
        "retMsg": str(response.get("retMsg") or "OK"),
        "result": response.get("data") if isinstance(response.get("data"), dict) else {},
        "retExtInfo": response.get("retExtInfo") or {},
        "time": int((response.get("header") or {}).get("Timenow") or time.time() * 1000),
    }


def _bitget_authenticator(
    api_key: str,
    secret_key: str,
    passphrase: str,
) -> Callable[[Any], None]:
    def authenticate(sock: Any) -> None:
        timestamp = str(int(time.time() * 1000))
        signature = base64.b64encode(
            hmac.new(
                secret_key.encode(),
                f"{timestamp}GET/user/verify".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        sock.send(
            _json_dumps(
                {
                    "op": "login",
                    "args": [
                        {
                            "apiKey": api_key,
                            "passphrase": passphrase,
                            "timestamp": timestamp,
                            "sign": signature,
                        }
                    ],
                }
            )
        )
        reply = json.loads(str(sock.recv()))
        if (
            not isinstance(reply, dict)
            or reply.get("event") != "login"
            or str(reply.get("code") or "") != "0"
        ):
            raise WebSocketOrderUnavailable("Bitget UTA WebSocket login failed")

    return authenticate


def submit_bitget_order(
    *,
    api_key: str,
    secret_key: str,
    passphrase: str,
    body: Dict[str, Any],
    simulated: bool,
    api_code: str,
) -> Optional[Dict[str, Any]]:
    if not websocket_order_entry_enabled():
        return None
    category = str(body.get("category") or "").strip().lower()
    if not category:
        raise LiveTradingError("Bitget UTA WebSocket order requires category")
    url = (
        "wss://wspap.bitget.com/v3/ws/private"
        if simulated
        else "wss://ws.bitget.com/v3/ws/private"
    )
    request_id = uuid.uuid4().hex
    request_time = str(int(time.time() * 1000))
    order_arg = {key: value for key, value in body.items() if key != "category"}
    if "reduceOnly" in order_arg:
        order_arg["reduceOnly"] = (
            "YES"
            if str(order_arg["reduceOnly"]).strip().lower() in {"yes", "true", "1"}
            else "NO"
        )
    if "takeProfit" in order_arg:
        order_arg["takeprofit"] = order_arg.pop("takeProfit")
    if "stopLoss" in order_arg:
        order_arg["stoploss"] = order_arg.pop("stopLoss")
    payload: Dict[str, Any] = {
        "op": "trade",
        "id": request_id,
        "category": category,
        "topic": "place-order",
        "requestTime": request_time,
        "args": [order_arg],
    }
    if api_code:
        payload["apiCode"] = str(api_code)
    conn = _connection(
        venue="bitget-uta",
        url=url,
        api_key=api_key,
        qualifier=f"{'demo' if simulated else 'live'}:{api_code}",
        authenticate=_bitget_authenticator(api_key, secret_key, passphrase),
    )
    try:
        response = conn.request(
            payload,
            matches=lambda msg: str(msg.get("id") or "") == request_id,
        )
    except WebSocketOrderUnavailable:
        return None
    if str(response.get("event") or "") == "error" or str(response.get("code") or "") not in {
        "0",
        "00000",
    }:
        raise LiveTradingError(f"Bitget UTA WebSocket order rejected: {response}")
    args = response.get("args")
    first = args[0] if isinstance(args, list) and args and isinstance(args[0], dict) else {}
    return {
        "code": "00000",
        "msg": str(response.get("msg") or "success"),
        "requestTime": int(response.get("ts") or request_time),
        "data": {
            "orderId": str(first.get("orderId") or ""),
            "clientOid": str(first.get("clientOid") or ""),
        },
    }


def _gate_signature(secret_key: str, channel: str, request_param: str, timestamp: int) -> str:
    message = f"api\n{channel}\n{request_param}\n{timestamp}"
    return hmac.new(secret_key.encode(), message.encode(), hashlib.sha512).hexdigest()


def _gate_authenticator(api_key: str, secret_key: str, channel: str) -> Callable[[Any], None]:
    def authenticate(sock: Any) -> None:
        timestamp = int(time.time())
        request_id = uuid.uuid4().hex
        sock.send(
            _json_dumps(
                {
                    "time": timestamp,
                    "channel": channel,
                    "event": "api",
                    "payload": {
                        "api_key": api_key,
                        "signature": _gate_signature(secret_key, channel, "", timestamp),
                        "timestamp": str(timestamp),
                        "req_id": request_id,
                    },
                }
            )
        )
        reply = json.loads(str(sock.recv()))
        status = str((reply.get("header") or {}).get("status") or "") if isinstance(reply, dict) else ""
        if not isinstance(reply, dict) or str(reply.get("request_id") or "") != request_id or status != "200":
            raise WebSocketOrderUnavailable("Gate WebSocket login failed")

    return authenticate


def submit_gate_order(
    *,
    api_key: str,
    secret_key: str,
    body: Dict[str, Any],
    base_url: str,
    futures: bool,
    channel_id: str,
) -> Optional[Dict[str, Any]]:
    if not websocket_order_entry_enabled():
        return None
    testnet = "testnet" in str(base_url or "").lower()
    if futures:
        url = (
            "wss://ws-testnet.gate.com/v4/ws/futures/usdt"
            if testnet
            else "wss://fx-ws.gateio.ws/v4/ws/usdt"
        )
        login_channel = "futures.login"
        order_channel = "futures.order_place"
    else:
        if testnet:
            return None
        url = "wss://api.gateio.ws/ws/v4/"
        login_channel = "spot.login"
        order_channel = "spot.order_place"
    request_id = uuid.uuid4().hex
    payload: Dict[str, Any] = {"req_id": request_id, "req_param": dict(body or {})}
    if channel_id:
        payload["header"] = {"x-gate-channel-id": str(channel_id)[:19]}
    conn = _connection(
        venue="gate-futures" if futures else "gate-spot",
        url=url,
        api_key=api_key,
        qualifier=channel_id,
        headers={"X-Gate-Size-Decimal": "1"},
        authenticate=_gate_authenticator(api_key, secret_key, login_channel),
    )
    try:
        response = conn.request(
            {
                "time": int(time.time()),
                "channel": order_channel,
                "event": "api",
                "payload": payload,
            },
            matches=lambda msg: str(msg.get("request_id") or "") == request_id,
            terminal=lambda msg: msg.get("ack") is not True,
        )
    except WebSocketOrderUnavailable:
        return None
    status = int((response.get("header") or {}).get("status") or 0)
    if status < 200 or status >= 300 or response.get("error"):
        raise LiveTradingError(f"Gate WebSocket order rejected: {response}")
    data = response.get("data") if isinstance(response, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, dict) else {"raw": result}
