import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.invoker import _build_rerun_agent_request  # noqa: E402
from service.conversation_context_service import ConversationRunContext  # noqa: E402


class RerunRequestContextTestCase(unittest.TestCase):
    def test_build_rerun_agent_request_keeps_database_context(self) -> None:
        runtime = ConversationRunContext(
            conversation=SimpleNamespace(id=105, insight_namespace_id=18),
            turn=SimpleNamespace(id=176, user_query="前天一共有多少个报警？看一下明细"),
            active_datasource_snapshot={},
            is_rerun=True,
            history_turn_limit=0,
        )

        request = _build_rerun_agent_request(
            username="wangshuzheng",
            runtime=runtime,
            auth_token="Bearer test-token",
            database_context={
                "host": "127.0.0.1",
                "port": "5432",
                "user": "fedquery",
                "password": "secret",
                "lake_rds_database_name": "lake_db",
            },
        )

        self.assertEqual(request.username, "wangshuzheng")
        self.assertEqual(request.namespace_id, "18")
        self.assertEqual(request.conversation_id, "105")
        self.assertEqual(request.user_message, "前天一共有多少个报警？看一下明细")
        self.assertEqual(request.auth_token, "Bearer test-token")
        self.assertEqual(request.database_context["lake_rds_database_name"], "lake_db")


if __name__ == "__main__":
    unittest.main()
