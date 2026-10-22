"""Quick Trade access to the shared exchange HTTP transport."""

from flask import g

from app.services.live_trading.http_transport import (
    get_exchange_http_session,
    release_exchange_http_session,
)


def open_quick_trade_transport() -> None:
    """Attach the current worker thread's reusable session to this request."""
    g.quick_trade_session = get_exchange_http_session()


def close_quick_trade_transport(error=None) -> None:
    """Drop request state while keeping the bounded connection pool warm."""
    g.pop("quick_trade_session", None)
    release_exchange_http_session()
