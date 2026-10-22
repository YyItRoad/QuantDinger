import pandas as pd

from app.services.backtest_cache import KlineCache


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    def get(self, key):
        return self.values.get(key)


def test_kline_cache_survives_process_memory_reset(monkeypatch):
    monkeypatch.setenv("REDIS_CACHE_NAMESPACE", "test-cache")
    redis = FakeRedis()
    writer = KlineCache()
    writer._redis_checked = True
    writer._redis = redis
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.5],
            "high": [102.0, 103.0],
            "low": [99.0, 100.5],
            "close": [101.5, 102.5],
            "volume": [12.0, 18.0],
        },
        index=pd.to_datetime(["2026-10-15T00:00:00Z", "2026-10-15T00:01:00Z"]),
    )

    writer.put("BTC/USDT:1m:range", frame, "1m")

    reader = KlineCache()
    reader._redis_checked = True
    reader._redis = redis
    restored = reader.get("BTC/USDT:1m:range")

    assert restored is not None
    assert list(restored.columns) == list(frame.columns)
    assert restored.index.tz is None
    assert restored["close"].tolist() == [101.5, 102.5]
    assert next(iter(redis.ttls.values())) == 21600


def test_kline_cache_ignores_corrupt_persistent_payload(monkeypatch):
    monkeypatch.setenv("REDIS_CACHE_NAMESPACE", "test-cache")
    redis = FakeRedis()
    cache = KlineCache()
    cache._redis_checked = True
    cache._redis = redis
    redis.values[cache._persistent_key("broken")] = b"not-zlib"

    assert cache.get("broken") is None
