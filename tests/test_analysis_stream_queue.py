import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.analysis_stream_queue import AnalysisStreamQueue
from service.analysis_stream_queue import RedisTimeoutError


class FakeRedisListClient:
    def __init__(self):
        self.values: dict[str, list[bytes]] = {}
        self.expirations: dict[str, int] = {}

    def pipeline(self):
        return FakeRedisPipeline(self)

    def blpop(self, key: str, timeout: int = 0):
        values = self.values.get(key) or []
        if not values:
            return None
        value = values.pop(0)
        return key.encode("utf-8"), value


class FakeRedisTimeoutClient(FakeRedisListClient):
    def blpop(self, key: str, timeout: int = 0):
        raise RedisTimeoutError("Timeout reading from socket")


class FakeRedisPipeline:
    def __init__(self, client: FakeRedisListClient):
        self.client = client
        self.commands = []

    def rpush(self, key: str, value: str):
        self.commands.append(("rpush", key, value))
        return self

    def ltrim(self, key: str, start: int, end: int):
        self.commands.append(("ltrim", key, start, end))
        return self

    def expire(self, key: str, seconds: int):
        self.commands.append(("expire", key, seconds))
        return self

    def execute(self):
        for command in self.commands:
            if command[0] == "rpush":
                _, key, value = command
                self.client.values.setdefault(key, []).append(value.encode("utf-8"))
            elif command[0] == "ltrim":
                _, key, start, end = command
                values = self.client.values.get(key, [])
                length = len(values)
                normalized_start = start if start >= 0 else max(length + start, 0)
                normalized_end = end if end >= 0 else length + end
                self.client.values[key] = values[normalized_start:normalized_end + 1]
            elif command[0] == "expire":
                _, key, seconds = command
                self.client.expirations[key] = seconds
        self.commands = []


class AnalysisStreamQueueTestCase(unittest.TestCase):
    def test_push_event_trims_oldest_messages_to_max_length(self):
        client = FakeRedisListClient()
        queue = AnalysisStreamQueue(client, max_len=2, event_max_bytes=1024, ttl_seconds=60)

        queue.push_event(9, {"type": "status", "message": "first"})
        queue.push_event(9, {"type": "status", "message": "second"})
        queue.push_event(9, {"type": "status", "message": "third"})

        stored = [json.loads(item.decode("utf-8")) for item in client.values["data-insight:{9}:events"]]
        self.assertEqual(["second", "third"], [item["message"] for item in stored])
        self.assertEqual(60, client.expirations["data-insight:{9}:events"])

    def test_push_event_truncates_oversized_message_content(self):
        client = FakeRedisListClient()
        queue = AnalysisStreamQueue(client, max_len=10, event_max_bytes=120, ttl_seconds=60)

        queue.push_event(9, {"type": "status", "message": "x" * 1000})

        stored = json.loads(client.values["data-insight:{9}:events"][0].decode("utf-8"))
        self.assertTrue(stored["truncated"])
        self.assertLessEqual(len(client.values["data-insight:{9}:events"][0]), 120)

    def test_pop_event_returns_none_when_queue_is_empty(self):
        client = FakeRedisListClient()
        queue = AnalysisStreamQueue(client, max_len=10, event_max_bytes=1024, ttl_seconds=60)

        self.assertIsNone(queue.pop_event(9, timeout_seconds=1))

    def test_pop_event_treats_redis_read_timeout_as_no_event(self):
        client = FakeRedisTimeoutClient()
        queue = AnalysisStreamQueue(client, max_len=10, event_max_bytes=1024, ttl_seconds=60)

        self.assertIsNone(queue.pop_event(9, timeout_seconds=30))


if __name__ == "__main__":
    unittest.main()
