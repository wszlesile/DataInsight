import json
from typing import Any

try:
    from redis.exceptions import TimeoutError as RedisTimeoutError
except Exception:  # pragma: no cover - redis may be absent in isolated unit tests.
    RedisTimeoutError = TimeoutError


class AnalysisStreamQueue:
    """Redis List backed transient SSE event queue for one analysis turn."""

    def __init__(
        self,
        redis_client: Any,
        *,
        max_len: int,
        event_max_bytes: int,
        ttl_seconds: int,
    ):
        self._redis = redis_client
        self._max_len = max(1, int(max_len or 1))
        self._event_max_bytes = max(64, int(event_max_bytes or 64))
        self._ttl_seconds = max(1, int(ttl_seconds or 1))

    def push_event(self, turn_id: int, event: dict[str, Any]) -> None:
        key = self.events_key(turn_id)
        payload = self._encode_event(event)
        pipeline = self._redis.pipeline()
        pipeline.rpush(key, payload)
        pipeline.ltrim(key, -self._max_len, -1)
        pipeline.expire(key, self._ttl_seconds)
        pipeline.execute()

    def pop_event(self, turn_id: int, timeout_seconds: int = 30) -> dict[str, Any] | None:
        try:
            item = self._redis.blpop(self.events_key(turn_id), timeout=int(timeout_seconds or 0))
        except RedisTimeoutError:
            return None
        if item is None:
            return None
        _, raw = item
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {
                "type": "message",
                "level": "warning",
                "message": str(raw),
            }

    def expire_turn(self, turn_id: int, ttl_seconds: int) -> None:
        self._redis.expire(self.events_key(turn_id), max(1, int(ttl_seconds or 1)))

    @staticmethod
    def events_key(turn_id: int) -> str:
        return f"data-insight:{{{int(turn_id)}}}:events"

    def _encode_event(self, event: dict[str, Any]) -> str:
        payload = dict(event or {})
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= self._event_max_bytes:
            return encoded.decode("utf-8")

        message = str(payload.get("message") or "")
        suffix = "...[truncated]"
        while message and len(encoded) > self._event_max_bytes:
            keep = max(0, len(message) - max(16, len(message) // 4))
            message = message[:keep]
            payload["message"] = f"{message}{suffix}"
            payload["truncated"] = True
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        if len(encoded) <= self._event_max_bytes:
            return encoded.decode("utf-8")

        fallback = {
            "type": payload.get("type") or "status",
            "level": "warning",
            "message": "event omitted",
            "truncated": True,
        }
        return json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
