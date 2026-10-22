"""Shared, bounded HTTP transport for native exchange REST clients."""

from __future__ import annotations

import atexit
import os
import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


_adapter_lock = threading.Lock()
_thread_local = threading.local()
_shared_adapter: Optional[HTTPAdapter] = None


def _bounded_int(name: str, default: int, *, minimum: int = 1, maximum: int = 256) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _new_adapter() -> HTTPAdapter:
    """Create a bounded pool with transport retries disabled for order safety."""
    retries = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0,
        status=0,
        allowed_methods=None,
        raise_on_status=False,
    )
    return HTTPAdapter(
        pool_connections=_bounded_int("LIVE_TRADING_HTTP_POOL_CONNECTIONS", 16),
        pool_maxsize=_bounded_int("LIVE_TRADING_HTTP_POOL_MAXSIZE", 32),
        max_retries=retries,
        pool_block=True,
    )


def get_exchange_http_adapter() -> HTTPAdapter:
    """Return the process-wide adapter that owns reusable connection pools."""
    global _shared_adapter
    if _shared_adapter is None:
        with _adapter_lock:
            if _shared_adapter is None:
                _shared_adapter = _new_adapter()
    return _shared_adapter


def get_exchange_http_session() -> requests.Session:
    """Return a thread-local session backed by the shared bounded pool."""
    adapter = get_exchange_http_adapter()
    session = getattr(_thread_local, "session", None)
    if session is None or getattr(_thread_local, "adapter", None) is not adapter:
        session = requests.Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
        _thread_local.adapter = adapter
    return session


def release_exchange_http_session() -> None:
    """Forget current-thread request state without closing shared pools."""
    session = getattr(_thread_local, "session", None)
    if session is not None:
        try:
            session.cookies.clear()
        except Exception:
            pass


def close_exchange_http_transport() -> None:
    """Close pooled sockets during process shutdown."""
    global _shared_adapter
    with _adapter_lock:
        adapter = _shared_adapter
        _shared_adapter = None
    if adapter is not None:
        adapter.close()
    for name in ("session", "adapter"):
        if hasattr(_thread_local, name):
            delattr(_thread_local, name)


atexit.register(close_exchange_http_transport)
