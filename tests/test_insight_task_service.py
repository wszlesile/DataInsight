import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config.database import Base  # noqa: E402
from insight_task.schedule import next_run_after, normalize_schedule  # noqa: E402
from insight_task.service import InsightTaskService  # noqa: E402
from model import (  # noqa: E402
    InsightDatasource,
    InsightNamespace,
    InsightNsConversation,
    InsightNsArtifact,
    InsightNsExecution,
    InsightNsTurn,
    InsightTaskDefinition,
    InsightTaskRevision,
    InsightTaskRun,
)
from utils.datasource_utils import dump_json  # noqa: E402


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _source_execution(session):
    namespace = InsightNamespace(username='owner', name='Operations')
    session.add(namespace)
    session.flush()
    datasource = InsightDatasource(
        insight_namespace_id=namespace.id,
        datasource_type='postgresql',
        datasource_name='Annual contracts',
    )
    session.add(datasource)
    session.flush()
    conversation = InsightNsConversation(insight_namespace_id=namespace.id, title='Contracts')
    session.add(conversation)
    session.flush()
    turn = InsightNsTurn(conversation_id=conversation.id, turn_no=1, user_query='分析年费合同', status='success')
    session.add(turn)
    session.flush()
    execution = InsightNsExecution(
        conversation_id=conversation.id,
        turn_id=turn.id,
        title='年费合同分析',
        description='按最新数据重新计算年费合同情况',
        generated_code="result = save_analysis_result(analysis_report='ok')",
        used_datasource_ids_json=dump_json([datasource.id]),
        execution_status='success',
        analysis_report='原始报告',
        result_payload_json=dump_json({
            'query_snapshots': {
                'annual_contracts': {
                    'query_key': 'annual_contracts',
                    'query_constraints': {
                        'time_range': None,
                        'target_filters': [{'field': 'contract_type', 'operator': '=', 'value': 'annual'}],
                        'aggregation': {'grain': 'contract', 'metrics': ['contract_count']},
                    },
                    'query_scope': {'datasource_ids': [datasource.id]},
                    'execution': {'status': 'success', 'row_count': 58},
                },
            },
        }),
        analysis_parameters_schema_json='{}',
        analysis_parameters_json='{}',
        execution_seconds=860,
        finished_at=datetime(2026, 8, 1, 1, 2, 3),
    )
    session.add(execution)
    session.flush()
    session.add_all([
        InsightNsArtifact(
            conversation_id=conversation.id,
            turn_id=turn.id,
            execution_id=execution.id,
            artifact_type='report',
            title='Annual contract report',
            summary_text='Artifact report',
            content_json=dump_json({'report_markdown': 'Artifact report'}),
            metadata_json=dump_json({'evidence_refs': {'58': 'ev-contract-count'}}),
            sort_no=0,
        ),
        InsightNsArtifact(
            conversation_id=conversation.id,
            turn_id=turn.id,
            execution_id=execution.id,
            artifact_type='chart',
            title='Contract trend',
            content_json=dump_json({'title': 'Contract trend', 'chart_type': 'line', 'chart_spec': {'series': []}}),
            metadata_json=dump_json({'evidence_refs': {'series.0': 'ev-contract-count'}}),
            sort_no=1,
        ),
        InsightNsArtifact(
            conversation_id=conversation.id,
            turn_id=turn.id,
            execution_id=execution.id,
            artifact_type='table',
            title='Contract details',
            content_json=dump_json({'title': 'Contract details', 'columns': [{'field': 'count'}], 'rows': [{'count': 58}]}),
            metadata_json=dump_json({'evidence_refs': {'count': 'ev-contract-count'}}),
            sort_no=101,
        ),
    ])
    session.commit()
    return execution


def test_schedule_is_structured_and_timezone_aware():
    schedule = normalize_schedule({
        'frequency': 'weekly',
        'weekdays': [1, 3],
        'time': '09:30',
        'timezone': 'Asia/Shanghai',
    })
    next_run = next_run_after(schedule, datetime(2026, 8, 9, 23, 0))
    assert next_run == datetime(2026, 8, 10, 1, 30)


def test_monthly_schedule_uses_the_last_day_when_requested_day_is_absent():
    schedule = normalize_schedule({
        'frequency': 'monthly',
        'day_of_month': 31,
        'time': '09:30',
        'timezone': 'Asia/Shanghai',
    })

    february_run = next_run_after(schedule, datetime(2026, 2, 1, 0, 0))
    march_run = next_run_after(schedule, datetime(2026, 2, 28, 2, 0))

    assert schedule['day_of_month'] == 31
    assert february_run == datetime(2026, 2, 28, 1, 30)
    assert march_run == datetime(2026, 3, 31, 1, 30)


@pytest.mark.parametrize('day_of_month', [0, 32])
def test_monthly_schedule_rejects_invalid_day(day_of_month):
    with pytest.raises(ValueError, match='1 到 31'):
        normalize_schedule({
            'frequency': 'monthly',
            'day_of_month': day_of_month,
            'time': '09:30',
            'timezone': 'Asia/Shanghai',
        })


def test_task_snapshot_is_private_and_fixed_parameter_analysis_is_runnable():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='每日年费合同洞察',
            description='每天刷新',
            schedule={'frequency': 'daily', 'time': '09:00', 'timezone': 'Asia/Shanghai'},
        )
        assert task['name'] == '每日年费合同洞察'
        assert task['revision']['snapshot']['default_parameters'] == {}
        assert task['revision']['snapshot']['datasource_bindings'][0]['datasource_name'] == 'Annual contracts'
        assert task['revision']['snapshot']['query_criteria'][0]['query_key'] == 'annual_contracts'
        assert 'code' not in task['revision']['snapshot']
        assert task['run_count'] == 1
        assert task['latest_run']['trigger_type'] == 'initial'
        assert task['latest_run']['status'] == 'success'
        assert len(task['runs']) == 1
        initial_run = service.get_run(
            username='owner',
            task_id=task['task_id'],
            run_id=task['runs'][0]['run_id'],
        )
        assert initial_run['trigger_type'] == 'initial'
        assert initial_run['execution_id'] == execution.id
        assert initial_run['analysis_report'] == 'Artifact report'
        assert initial_run['result_payload']['charts'][0]['title'] == 'Contract trend'
        assert initial_run['result_payload']['charts'][0]['evidence_refs']['series.0'] == 'ev-contract-count'
        assert initial_run['result_payload']['tables'][0]['rows'][0]['count'] == 58
        assert initial_run['result_payload']['report_evidence_refs']['58'] == 'ev-contract-count'
        assert initial_run['execution_seconds'] == 860
        assert initial_run['scheduled_for'] == '2026-07-31T17:02:03Z'
        assert session.query(InsightTaskDefinition).count() == 1
        assert session.query(InsightTaskRevision).count() == 1
        assert session.query(InsightTaskRun).count() == 1
    finally:
        session.close()


def test_manual_run_uses_shared_runtime_and_persists_independent_history():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='合同洞察',
            description='',
            schedule={'frequency': 'manual', 'timezone': 'Asia/Shanghai'},
        )
        queued = service.enqueue_manual_run(username='owner', task_id=task['task_id'], parameters={})

        def run_locally(**kwargs):
            audit = InsightNsExecution(
                conversation_id=kwargs['conversation_id'],
                turn_id=kwargs['turn_id'],
                generated_code=kwargs['code'],
                execution_status='success',
                analysis_report='当前共 58 份年费合同',
                result_payload_json='{}',
                analysis_parameters_schema_json=dump_json(kwargs['analysis_parameters_schema']),
                analysis_parameters_json=dump_json(kwargs['analysis_parameters']),
                execution_source=kwargs['execution_source'],
            )
            session.add(audit)
            session.flush()
            return {
                'execution_id': audit.id,
                'status': 'success',
                'analysis_report': audit.analysis_report,
                'charts': [{'title': '合同趋势', 'chart_spec': {}}],
                'tables': [{'title': '合同明细', 'columns': [], 'rows': []}],
                'query_snapshots': {
                    'annual_contracts': {
                        'query_key': 'annual_contracts',
                        'query_constraints': {'aggregation': {'grain': 'contract'}},
                        'execution': {'status': 'success', 'row_count': 58},
                    },
                },
                'analysis_parameters_schema': kwargs['analysis_parameters_schema'],
                'analysis_parameters': kwargs['analysis_parameters'],
                'execution_seconds': 1200,
            }

        with (
            patch('insight_task.service.run_generated_code_locally', side_effect=run_locally),
            patch(
                'service.post_analysis_evidence_service.PostAnalysisEvidenceCompiler.compile',
                side_effect=AssertionError('scheduled local execution must not compile Evidence'),
            ),
        ):
            result = service.execute_run(
                run_id=queued['run_id'],
                execution_principal='DataInsight-Server',
                runtime_user_context={'fedquery_authorization': 'Bearer jwt'},
                worker_id='test-worker',
            )

        assert result['status'] == 'success'
        assert result['analysis_report'] == '当前共 58 份年费合同'
        assert result['result_payload']['charts'][0]['title'] == '合同趋势'
        assert result['result_payload']['query_snapshots']['annual_contracts']['execution']['row_count'] == 58
        assert result['execution_principal'] == 'DataInsight-Server'
        assert session.query(InsightTaskRun).count() == 2
        audit = session.query(InsightNsExecution).filter(InsightNsExecution.id == result['execution_id']).first()
        assert audit.content_enabled == 0
        original_turn = session.query(InsightNsTurn).filter(InsightNsTurn.id == execution.turn_id).first()
        assert original_turn.status == 'success'
    finally:
        session.close()


def test_manual_run_executes_with_current_user_context_and_is_not_runner_work():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='Manual user execution',
            description='',
            schedule={'frequency': 'daily', 'time': '09:00', 'timezone': 'Asia/Shanghai'},
        )
        runtime_context = {
            'platform_authorization': 'Bearer user-ticket',
            'fedquery_authorization': 'Bearer user-jwt',
        }

        with patch(
            'insight_task.service.run_generated_code_locally',
            return_value={
                'status': 'success',
                'analysis_report': 'manual result',
                'execution_seconds': 25,
            },
        ) as execute_code:
            result = service.execute_manual_run(
                username='owner',
                task_id=task['task_id'],
                parameters={},
                runtime_user_context=runtime_context,
                worker_id='api:manual',
            )

        assert result['trigger_type'] == 'manual'
        assert result['status'] == 'success'
        assert result['execution_principal'] == 'owner'
        assert result['worker_id'] == 'api:manual'
        call = execute_code.call_args.kwargs
        assert call['runtime_user_context'] == runtime_context
        assert call['execution_source'] == 'manual_task'

        queued_manual = service.enqueue_manual_run(
            username='owner',
            task_id=task['task_id'],
            parameters={},
        )
        assert queued_manual['status'] == 'queued'
        assert service.list_queued_runs() == []
    finally:
        session.close()


def test_recovery_requeues_scheduled_runs_but_fails_interrupted_manual_runs():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='Recovery boundaries',
            description='',
            schedule={'frequency': 'daily', 'time': '09:00', 'timezone': 'Asia/Shanghai'},
        )
        revision = session.query(InsightTaskRevision).filter(
            InsightTaskRevision.task_definition_id == task['id'],
        ).one()
        manual = InsightTaskRun(
            run_id='manual-stale',
            task_definition_id=task['id'],
            revision_id=revision.id,
            trigger_type='manual',
            scheduled_for=datetime.utcnow(),
            status='running',
            lease_until=datetime.utcnow() - timedelta(seconds=1),
        )
        scheduled = InsightTaskRun(
            run_id='scheduled-stale',
            task_definition_id=task['id'],
            revision_id=revision.id,
            trigger_type='scheduled',
            scheduled_for=datetime.utcnow() + timedelta(seconds=1),
            status='running',
            lease_until=datetime.utcnow() - timedelta(seconds=1),
        )
        session.add_all([manual, scheduled])
        session.commit()

        assert service.recover_expired_runs() == 2
        session.refresh(manual)
        session.refresh(scheduled)
        assert manual.status == 'failed'
        assert manual.error_message == '手动执行请求已中断，请重新执行'
        assert scheduled.status == 'queued'
        assert scheduled.error_message == ''
    finally:
        session.close()


def test_failed_task_execution_is_hidden_from_source_turn():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='失败隔离测试',
            description='',
            schedule={'frequency': 'manual', 'timezone': 'Asia/Shanghai'},
        )
        queued = service.enqueue_manual_run(username='owner', task_id=task['task_id'], parameters={})

        def fail_locally(**kwargs):
            audit = InsightNsExecution(
                conversation_id=kwargs['conversation_id'],
                turn_id=kwargs['turn_id'],
                generated_code=kwargs['code'],
                execution_status='failed',
                error_message='query failed',
                result_payload_json='{}',
                execution_source=kwargs['execution_source'],
            )
            session.add(audit)
            session.flush()
            return {'execution_id': audit.id, 'status': 'failed', 'error_message': 'query failed'}

        with patch('insight_task.service.run_generated_code_locally', side_effect=fail_locally):
            result = service.execute_run(
                run_id=queued['run_id'],
                execution_principal='DataInsight-Server',
                runtime_user_context={'fedquery_authorization': 'Bearer jwt'},
                worker_id='test-worker',
            )

        assert result['status'] == 'failed'
        audit = session.query(InsightNsExecution).filter(InsightNsExecution.id == result['execution_id']).first()
        assert audit.content_enabled == 0
    finally:
        session.close()


def test_retryable_dispatch_failure_uses_bounded_persistent_backoff():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='派发重试测试',
            description='',
            schedule={'frequency': 'manual', 'timezone': 'Asia/Shanghai'},
        )
        queued = service.enqueue_manual_run(username='owner', task_id=task['task_id'], parameters={})

        with (
            patch('insight_task.service.Config.INSIGHT_TASK_DISPATCH_MAX_ATTEMPTS', 2),
            patch('insight_task.service.Config.INSIGHT_TASK_DISPATCH_RETRY_BASE_SECONDS', 5),
        ):
            before = datetime.utcnow()
            service.record_dispatch_failure(
                run_id=queued['run_id'],
                error_message='gateway unavailable',
                retryable=True,
            )
            run = session.query(InsightTaskRun).filter(InsightTaskRun.run_id == queued['run_id']).first()
            assert run.status == 'queued'
            assert run.dispatch_attempts == 1
            assert run.next_attempt_at >= before + timedelta(seconds=4)
            assert service.list_queued_runs() == []

            run.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            session.commit()
            service.record_dispatch_failure(
                run_id=queued['run_id'],
                error_message='gateway unavailable again',
                retryable=True,
            )
            session.refresh(run)
            assert run.status == 'failed'
            assert run.dispatch_attempts == 2
            assert run.next_attempt_at is None
    finally:
        session.close()


def test_run_history_is_pageable():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='历史分页测试',
            description='',
            schedule={'frequency': 'manual', 'timezone': 'Asia/Shanghai'},
        )
        for _ in range(3):
            service.enqueue_manual_run(username='owner', task_id=task['task_id'], parameters={})
        page = service.list_runs_page(username='owner', task_id=task['task_id'], page_no=2, page_size=2)
        assert page['total'] == 4
        assert len(page['items']) == 2
        assert page['has_more'] is False
    finally:
        session.close()


def test_due_schedule_is_enqueued_once_and_advanced():
    session = _session()
    try:
        execution = _source_execution(session)
        service = InsightTaskService(session)
        task_data = service.create_task(
            username='owner',
            source_execution_id=execution.id,
            name='合同日报',
            description='',
            schedule={'frequency': 'daily', 'time': '09:00', 'timezone': 'Asia/Shanghai'},
        )
        task = session.query(InsightTaskDefinition).filter(
            InsightTaskDefinition.task_id == task_data['task_id'],
        ).first()
        task.next_run_at = datetime(2026, 1, 1, 1, 0)
        session.commit()

        first = service.enqueue_due_runs()
        second = service.enqueue_due_runs()

        assert len(first) == 1
        assert second == []
        assert session.query(InsightTaskRun).count() == 2
        session.refresh(task)
        assert task.next_run_at > datetime(2026, 1, 1, 1, 0)
    finally:
        session.close()
