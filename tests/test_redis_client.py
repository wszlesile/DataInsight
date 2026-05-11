import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from utils import redis_client


class RedisClientTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        redis_client.get_redis_client.cache_clear()

    def test_parse_cluster_nodes_supports_comma_separated_hosts(self):
        self.assertEqual(
            [("redis-a", 6379), ("redis-b", 6380)],
            redis_client._parse_cluster_nodes("redis-a:6379,redis-b:6380"),
        )

    def test_get_redis_client_falls_back_to_standalone_when_cluster_init_fails(self):
        standalone = Mock()
        with (
            patch.object(redis_client.Config, "SUPOS_REDIS_CLUSTER_NODES", "nocode-redis:6379"),
            patch.object(redis_client.Config, "SUPOS_REDIS_PASSWORD", ""),
            patch("redis.cluster.RedisCluster", side_effect=Exception("cluster disabled")),
            patch("redis.Redis", return_value=standalone),
        ):
            client = redis_client.get_redis_client()

        self.assertIs(standalone, client)


if __name__ == "__main__":
    unittest.main()
