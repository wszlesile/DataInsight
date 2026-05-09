import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config.database import Base
from model import InsightNsExecution, InsightNsMessage, InsightNsTurn
from service.conversation_context_service import ConversationContextService, _attach_chart_artifact_refs


class ConversationContextServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.session = session_factory()

    def tearDown(self) -> None:
        self.session.close()

    def _add_turn_messages(
        self,
        *,
        turn_id: int,
        turn_no: int,
        user_content: str,
        assistant_content: str,
    ) -> None:
        self.session.add(InsightNsTurn(
            id=turn_id,
            conversation_id=17,
            turn_no=turn_no,
            user_query=user_content,
            selected_datasource_ids_json='[]',
            selected_datasource_snapshot_json='[]',
            final_answer=assistant_content,
            status='success',
            error_message='',
            is_deleted=0,
        ))
        self.session.add(InsightNsMessage(
            id=turn_id * 2 - 1,
            insight_namespace_id=4,
            insight_conversation_id=17,
            turn_id=turn_id,
            turn_no=turn_no,
            seq_no=1,
            role='user',
            message_kind='prompt',
            content=user_content,
            content_json='{}',
            is_deleted=0,
        ))
        self.session.add(InsightNsMessage(
            id=turn_id * 2,
            insight_namespace_id=4,
            insight_conversation_id=17,
            turn_id=turn_id,
            turn_no=turn_no,
            seq_no=2,
            role='assistant',
            message_kind='final_answer',
            content=assistant_content,
            content_json='{}',
            is_deleted=0,
        ))

    def _add_execution(self, *, execution_id: int, turn_id: int, status: str) -> None:
        self.session.add(InsightNsExecution(
            id=execution_id,
            conversation_id=17,
            turn_id=turn_id,
            tool_call_id=f'tool-{execution_id}',
            title='analysis',
            description='analysis',
            generated_code='print(1)',
            execution_status=status,
            analysis_report='report' if status == 'success' else '',
            result_payload_json='{}',
            stdout_text='',
            stderr_text='',
            execution_seconds=1,
            error_message='' if status == 'success' else 'failed',
            is_deleted=0,
        ))

    def test_recent_prompt_messages_replays_plain_text_turn_without_execution_as_pair(self) -> None:
        self._add_turn_messages(
            turn_id=33,
            turn_no=7,
            user_content='what model are you using',
            assistant_content='I cannot access model configuration details.',
        )
        self.session.commit()

        messages = ConversationContextService(self.session).get_recent_prompt_messages(
            conversation_id=17,
            limit_messages=10,
            max_turn_no=7,
        )

        self.assertEqual([message.role for message in messages], ['user', 'assistant'])
        self.assertEqual(messages[0].content, 'what model are you using')
        self.assertEqual(messages[1].content, 'I cannot access model configuration details.')

    def test_recent_prompt_messages_filters_failed_analysis_assistant(self) -> None:
        self._add_turn_messages(
            turn_id=34,
            turn_no=8,
            user_content='analyze data quality',
            assistant_content='I will inspect the file and generate code.',
        )
        self._add_execution(execution_id=1, turn_id=34, status='failed')
        self.session.commit()

        messages = ConversationContextService(self.session).get_recent_prompt_messages(
            conversation_id=17,
            limit_messages=10,
            max_turn_no=8,
        )

        self.assertEqual([message.role for message in messages], ['user'])
        self.assertEqual(messages[0].content, 'analyze data quality')

    def test_recent_prompt_messages_keeps_successful_analysis_answer(self) -> None:
        self._add_turn_messages(
            turn_id=35,
            turn_no=9,
            user_content='analyze oee by equipment',
            assistant_content='OEE analysis report',
        )
        self._add_execution(execution_id=2, turn_id=35, status='success')
        self.session.commit()

        messages = ConversationContextService(self.session).get_recent_prompt_messages(
            conversation_id=17,
            limit_messages=10,
            max_turn_no=9,
        )

        self.assertEqual([message.role for message in messages], ['user', 'assistant'])
        self.assertEqual(messages[1].content, 'OEE analysis report')

    def test_attach_chart_artifact_refs_backfills_chart_ids(self) -> None:
        charts = [
            {"title": "chart1", "chart_type": "echarts", "chart_spec": {"series": []}},
            {"title": "chart2", "chart_type": "echarts", "chart_spec": {"series": []}},
        ]
        artifacts = [
            {"id": 101, "artifact_type": "chart"},
            {"id": 102, "artifact_type": "chart"},
        ]

        merged = _attach_chart_artifact_refs(charts, artifacts)

        self.assertEqual(merged[0]["id"], 101)
        self.assertEqual(merged[1]["id"], 102)
        self.assertNotIn("chart_artifact_id", merged[0])
        self.assertNotIn("chart_artifact_id", merged[1])

    def test_attach_chart_artifact_refs_preserves_existing_chart_payload(self) -> None:
        charts = [
            {"title": "chart1", "description": "desc", "chart_type": "echarts", "chart_spec": {"series": [1]}},
        ]
        artifacts = [{"id": 201, "artifact_type": "chart"}]

        merged = _attach_chart_artifact_refs(charts, artifacts)

        self.assertEqual(merged[0]["title"], "chart1")
        self.assertEqual(merged[0]["description"], "desc")
        self.assertEqual(merged[0]["chart_spec"], {"series": [1]})
        self.assertEqual(merged[0]["id"], 201)


if __name__ == "__main__":
    unittest.main()
