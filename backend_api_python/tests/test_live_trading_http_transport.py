import threading

import pytest

from app.services.live_trading import http_transport


@pytest.fixture(autouse=True)
def _reset_transport():
    http_transport.close_exchange_http_transport()
    yield
    http_transport.close_exchange_http_transport()


def test_pool_is_bounded_and_never_retries_orders(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_HTTP_POOL_CONNECTIONS", "7")
    monkeypatch.setenv("LIVE_TRADING_HTTP_POOL_MAXSIZE", "11")

    adapter = http_transport.get_exchange_http_adapter()

    assert adapter._pool_connections == 7
    assert adapter._pool_maxsize == 11
    assert adapter._pool_block is True
    assert adapter.max_retries.total == 0
    assert adapter.max_retries.allowed_methods is None


def test_sessions_are_thread_local_while_connection_pool_is_shared():
    main_session = http_transport.get_exchange_http_session()
    captured = {}

    def read_worker_transport():
        captured["session"] = http_transport.get_exchange_http_session()
        captured["adapter"] = captured["session"].adapters["https://"]

    worker = threading.Thread(target=read_worker_transport)
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert captured["session"] is not main_session
    assert captured["adapter"] is main_session.adapters["https://"]


def test_same_thread_reuses_session():
    first = http_transport.get_exchange_http_session()
    second = http_transport.get_exchange_http_session()

    assert second is first
