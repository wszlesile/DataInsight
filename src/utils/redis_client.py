from functools import lru_cache

from config import Config


def _parse_cluster_nodes(nodes_text: str) -> list[tuple[str, int]]:
    nodes: list[tuple[str, int]] = []
    for item in str(nodes_text or '').split(','):
        item = item.strip()
        if not item:
            continue
        host, _, port_text = item.partition(':')
        nodes.append((host.strip(), int(port_text or 6379)))
    return nodes


@lru_cache(maxsize=1)
def get_redis_client():
    nodes = _parse_cluster_nodes(Config.SUPOS_REDIS_CLUSTER_NODES)
    if not nodes:
        raise RuntimeError('SUPOS_REDIS_CLUSTER_NODES 未配置，无法使用流式分析队列')

    try:
        from redis.cluster import ClusterNode, RedisCluster
        from redis import Redis
    except Exception as exc:
        raise RuntimeError('缺少 redis 依赖，无法连接平台 Redis 集群') from exc

    common_kwargs = {
        "password": Config.SUPOS_REDIS_PASSWORD or None,
        "socket_connect_timeout": Config.REDIS_CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": max(
            float(Config.REDIS_SOCKET_TIMEOUT_SECONDS),
            float(Config.STREAM_BLPOP_TIMEOUT_SECONDS) + 1,
        ),
        "decode_responses": False,
    }
    try:
        return RedisCluster(
            startup_nodes=[ClusterNode(host, port) for host, port in nodes],
            **common_kwargs,
        )
    except Exception:
        if len(nodes) != 1:
            raise
        host, port = nodes[0]
        return Redis(host=host, port=port, **common_kwargs)
