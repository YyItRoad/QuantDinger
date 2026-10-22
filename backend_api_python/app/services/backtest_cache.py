import hashlib
import io
import os
import threading
import time
import zlib
from typing import Any, Dict, Optional

import pandas as pd

from app.config.redis_urls import cache_key, cache_redis_url
from app.utils.logger import get_logger


logger = get_logger(__name__)


class KlineCache:
    """Process-local and Redis-backed K-line cache with timeframe-aware TTL."""

    def __init__(self, max_size: int = 64):
        self._store: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._max_size = max_size
        self._redis = None
        self._redis_checked = False

    @staticmethod
    def _ttl_for_timeframe(timeframe: str) -> int:
        if timeframe in ("1m", "5m", "15m", "30m"):
            return 21600
        return 86400

    @staticmethod
    def _redis_enabled() -> bool:
        return os.getenv("CACHE_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }

    def _redis_client(self):
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        if not self._redis_enabled():
            return None
        try:
            import redis

            self._redis = redis.Redis.from_url(
                cache_redis_url(),
                decode_responses=False,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
            self._redis.ping()
        except Exception as exc:
            logger.info("Persistent backtest cache unavailable; using memory cache: %s", exc)
            self._redis = None
        return self._redis

    @staticmethod
    def _persistent_key(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return cache_key(f"backtest:kline:v1:{digest}")

    @staticmethod
    def _encode(df: pd.DataFrame) -> bytes:
        payload = df.to_json(orient="table", date_format="iso", double_precision=15)
        return zlib.compress(payload.encode("utf-8"), level=6)

    @staticmethod
    def _decode(payload: bytes) -> pd.DataFrame:
        text = zlib.decompress(payload).decode("utf-8")
        frame = pd.read_json(io.StringIO(text), orient="table")
        frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
        return frame.sort_index()

    def get(self, key: str) -> Optional[pd.DataFrame]:
        with self._lock:
            entry = self._store.get(key)
            if entry is not None and time.time() > entry["expires"]:
                del self._store[key]
                entry = None
            if entry is not None:
                return entry["df"].copy()

        client = self._redis_client()
        if client is None:
            return None
        try:
            payload = client.get(self._persistent_key(key))
            if not payload:
                return None
            frame = self._decode(payload)
            with self._lock:
                self._remember(key, frame, self._ttl_for_timeframe("1m"))
            return frame.copy()
        except Exception as exc:
            logger.warning("Persistent backtest cache read failed: %s", exc)
            return None

    def put(self, key: str, df: pd.DataFrame, timeframe: str) -> None:
        ttl = self._ttl_for_timeframe(timeframe)
        with self._lock:
            self._remember(key, df, ttl)
        client = self._redis_client()
        if client is None:
            return
        try:
            client.setex(self._persistent_key(key), ttl, self._encode(df))
        except Exception as exc:
            logger.warning("Persistent backtest cache write failed: %s", exc)

    def _remember(self, key: str, df: pd.DataFrame, ttl: int) -> None:
        if len(self._store) >= self._max_size and key not in self._store:
            oldest_key = min(self._store, key=lambda item: self._store[item]["expires"])
            del self._store[oldest_key]
        self._store[key] = {
            "df": df.copy(),
            "expires": time.time() + ttl,
        }
