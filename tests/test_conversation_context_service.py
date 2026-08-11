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
from model import (
    InsightDatasource,
    InsightNamespace,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsMessage,
    InsightNsRelDatasource,
    InsightNsTurn,
)
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

    def _add_execution(
        self,
        *,
        execution_id: int,
        turn_id: int,
        status: str,
        analysis_task_id: int | None = None,
    ) -> None:
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
            analysis_task_id=analysis_task_id,
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

    def test_turn_executions_can_be_scoped_to_current_analysis_task(self) -> None:
        self._add_execution(
            execution_id=11,
            turn_id=40,
            status='failed',
            analysis_task_id=1001,
        )
        self._add_execution(
            execution_id=12,
            turn_id=40,
            status='failed',
            analysis_task_id=1002,
        )
        self.session.commit()

        executions = ConversationContextService(self.session).get_turn_executions(
            turn_id=40,
            analysis_task_id=1002,
        )
        latest = ConversationContextService(self.session).get_latest_execution(
            turn_id=40,
            analysis_task_id=1002,
        )

        self.assertEqual([execution.id for execution in executions], [12])
        self.assertEqual(latest.id, 12)

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

    def test_fail_run_does_not_preserve_prior_error_as_active_result(self) -> None:
        prior_error = (
            '(psycopg2.OperationalError) server closed the connection unexpectedly\n'
            '(Background on this error at: https://sqlalche.me/e/20/e3q8)'
        )
        self.session.add(InsightNsConversation(
            id=41,
            insight_namespace_id=4,
            title='analysis',
            status='active',
            summary_text='',
            active_datasource_snapshot='{}',
            last_turn_no=1,
            user_message='analyze',
            insight_result=prior_error,
        ))
        self.session.add(InsightNsTurn(
            id=71,
            conversation_id=41,
            turn_no=1,
            user_query='analyze',
            selected_datasource_ids_json='[]',
            selected_datasource_snapshot_json='[]',
            final_answer=prior_error,
            status='running',
            error_message='',
            is_deleted=0,
        ))
        self.session.add(InsightNsMessage(
            id=7101,
            insight_namespace_id=4,
            insight_conversation_id=41,
            turn_id=71,
            turn_no=1,
            seq_no=1,
            role='assistant',
            message_kind='error',
            content=prior_error,
            content_json='{}',
            is_deleted=0,
        ))
        self.session.commit()

        ConversationContextService(self.session).fail_run(
            conversation_id=41,
            turn_id=71,
            error_message='llm gateway timeout',
            preserve_existing_results=True,
        )

        turn = self.session.query(InsightNsTurn).filter_by(id=71).one()
        self.assertEqual('failed', turn.status)
        self.assertEqual('llm gateway timeout', turn.final_answer)
        self.assertEqual('llm gateway timeout', turn.error_message)

    def test_fail_run_preserves_legacy_final_answer_result(self) -> None:
        self.session.add(InsightNsConversation(
            id=42,
            insight_namespace_id=4,
            title='analysis',
            status='active',
            summary_text='',
            active_datasource_snapshot='{}',
            last_turn_no=1,
            user_message='analyze',
            insight_result='legacy report',
        ))
        self.session.add(InsightNsTurn(
            id=72,
            conversation_id=42,
            turn_no=1,
            user_query='analyze',
            selected_datasource_ids_json='[]',
            selected_datasource_snapshot_json='[]',
            final_answer='legacy report',
            status='running',
            error_message='',
            is_deleted=0,
        ))
        self.session.commit()

        ConversationContextService(self.session).fail_run(
            conversation_id=42,
            turn_id=72,
            error_message='llm gateway timeout',
            preserve_existing_results=True,
        )

        turn = self.session.query(InsightNsTurn).filter_by(id=72).one()
        self.assertEqual('success', turn.status)
        self.assertEqual('legacy report', turn.final_answer)
        self.assertEqual('', turn.error_message)

    def test_start_rerun_refreshes_current_datasource_bindings(self) -> None:
        self.session.add(InsightNamespace(
            id=5,
            username='admin',
            name='默认空间',
            is_deleted=0,
        ))
        self.session.add(InsightNsConversation(
            id=109,
            insight_namespace_id=5,
            title='analysis',
            status='active',
            summary_text='',
            active_datasource_snapshot='{}',
            last_turn_no=1,
            user_message='统计一下',
            insight_result='当前会话还没有关联数据源',
            is_deleted=0,
        ))
        self.session.add(InsightNsTurn(
            id=901,
            conversation_id=109,
            turn_no=1,
            user_query='统计一下',
            selected_datasource_ids_json='[]',
            selected_datasource_snapshot_json='[]',
            final_answer='当前会话还没有关联数据源',
            status='success',
            error_message='',
            is_deleted=0,
        ))
        self.session.add(InsightDatasource(
            id=301,
            insight_namespace_id=5,
            datasource_type='table',
            datasource_name='产量表',
            knowledge_tag='',
            datasource_schema='{"properties":{"产量":{"type":"number"}}}',
            datasource_config_json='{"table_name":"public.output"}',
            is_deleted=0,
        ))
        self.session.add(InsightNsRelDatasource(
            insight_namespace_id=5,
            insight_conversation_id=109,
            datasource_id=301,
            sort_no=1,
            bind_source='user_selected',
            is_deleted=0,
        ))
        self.session.commit()

        runtime = ConversationContextService(self.session).start_rerun(
            username='admin',
            conversation_id=109,
            turn_id=901,
        )

        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.active_datasource_snapshot['selected_datasource_ids'], [301])
        self.assertEqual(
            runtime.active_datasource_snapshot['selected_datasource_snapshot'][0]['datasource_name'],
            '产量表',
        )
        turn = self.session.query(InsightNsTurn).filter_by(id=901).one()
        self.assertEqual(turn.selected_datasource_ids_json, '[301]')
        self.assertIn('产量表', turn.selected_datasource_snapshot_json)


if __name__ == "__main__":
    unittest.main()
