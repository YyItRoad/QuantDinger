"""币安账户快照解析测试。"""

from app.services.live_trading.account_snapshot import _parse_binance_futures_positions


def test_parse_binance_futures_position_keeps_mark_price():
    rows = _parse_binance_futures_positions(
        [
            {
                "symbol": "KAITOUSDC",
                "positionAmt": "422.5",
                "entryPrice": "0.355",
                "markPrice": "0.3612",
                "leverage": "10",
            }
        ]
    )

    assert len(rows) == 1
    assert rows[0]["entry_price"] == 0.355
    assert rows[0]["mark_price"] == 0.3612
    assert rows[0]["leverage"] == 10
