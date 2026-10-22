from __future__ import annotations

from urllib.parse import urlencode

import pytest

from app.services.live_trading.bitget import BitgetMixClient
from app.services.live_trading.bitget_spot import BitgetSpotClient


@pytest.mark.parametrize("client_cls", [BitgetMixClient, BitgetSpotClient])
def test_bitget_signed_request_sends_the_same_query_order_it_signs(monkeypatch, client_cls):
    client = client_cls(api_key="key", secret_key="secret", passphrase="pass")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return 200, {"code": "00000", "data": {}}, "{}"

    monkeypatch.setattr(client, "_request", fake_request)
    module = "bitget_spot" if client_cls is BitgetSpotClient else "bitget"
    monkeypatch.setattr(f"app.services.live_trading.{module}.time.time", lambda: 1_700_000_000)

    client._signed_request(
        "GET",
        "/api/v3/account/fee-rate",
        params={"symbol": "btcusdt", "category": "USDT-FUTURES"},
    )

    sent_params = captured["params"]
    assert list(sent_params) == ["category", "symbol"]
    query = urlencode(list(sent_params.items()))
    expected = client._sign(
        "1700000000000",
        "GET",
        f"/api/v3/account/fee-rate?{query}",
        "",
    )
    assert captured["headers"]["ACCESS-SIGN"] == expected
