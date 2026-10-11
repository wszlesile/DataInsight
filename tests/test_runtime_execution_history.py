import ast
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from agent import invoker, tools
from model import InsightNsExecution, InsightNsTurn


@pytest.fixture
def history():
    engine = create_engine('sqlite://')
    InsightNsExecution.__table__.create(engine)
    InsightNsTurn.__table__.create(engine)
    sessions = sessionmaker(bind=engine)

    def add(execution_id, task=10, signal='no_data_found', snapshots=None, status='failed', **kwargs):
        with sessions() as session:
            session.add(InsightNsExecution(
                id=execution_id, conversation_id=1, turn_id=1,
                analysis_task_id=task, execution_status=status,
                error_message=f'{signal}: attempt {execution_id}',
                result_payload_json=json.dumps({
                    'error_type': signal,
                    'query_snapshots': snapshots or {},
                }), **kwargs,
            ))
            session.commit()

    with patch('config.database.SessionLocal', sessions):
        yield add, sessions
    engine.dispose()


def query_fact(rows=0, status='success', physical=True):
    return {
        'query_scope': {'time_range': {'start': '2026-08-01', 'end': '2026-08-02'}},
        'implementation': {'physical_execution': physical, 'sql': 'SELECT * FROM readings'},
        'execution': {'status': status, 'row_count': rows},
    }


def test_query_identity_excludes_execution_observations_and_retains_receipts():
    from copy import deepcopy

    token = tools.CURRENT_QUERY_SNAPSHOTS.set({})
    last = tools.CURRENT_LAST_QUERY_KEY.set('')
    try:
        declaration = {'query_key': 'readings', 'sql': 'SELECT value FROM readings',
                       'query_constraints': {'time_range': None, 'target_filters': [], 'aggregation': None}}
        assert tools._register_query_snapshot(**declaration) is None
        snapshot = tools.CURRENT_QUERY_SNAPSHOTS.get()['readings']
        snapshot['implementation']['physical_execution'] = True
        snapshot['implementation']['physical_executions'] = [{'row_count': 2}]
        snapshot['execution'] = {'status': 'success', 'row_count': 2}
        completed = deepcopy(snapshot)
        assert tools._register_query_snapshot(**declaration) is None
        assert tools.CURRENT_QUERY_SNAPSHOTS.get()['readings'] == completed
        changed = dict(declaration, sql='SELECT value FROM readings WHERE value > 2')
        retry = tools._register_query_snapshot(**changed)
        assert retry is not None
        assert tools.CURRENT_QUERY_SNAPSHOTS.get()['readings'] == completed
    finally:
        tools.CURRENT_QUERY_SNAPSHOTS.reset(token)
        tools.CURRENT_LAST_QUERY_KEY.reset(last)


def test_all_history_readers_exclude_other_tasks_future_and_deleted_executions(history):
    add, _ = history
    add(1, task=9, snapshots={'old': query_fact()})
    add(2, signal='probe_feedback', snapshots={'probe': query_fact(3)})
    add(3, signal='undefined_name')
    add(4, snapshots={'current_scope': query_fact()})
    add(5, status='running', signal='')
    add(6, snapshots={'future': query_fact()})
    add(7, task=9)
    add(8, is_deleted=1)
    assert tools._count_probe_no_data_failures(1, 5) == 1
    memory = tools._build_turn_failure_memory(1, 5)
    assert memory['previous_failure_messages'] == ['undefined_name: attempt 3', 'no_data_found: attempt 4']
    snapshots = tools._load_confirmed_no_data_query_snapshots(turn_id=1, current_execution_id=5)
    assert list(snapshots) == ['confirmed_no_data_4_current_scope']
    assert tools._turn_has_prior_time_range_guard_signal(1, 5)


def test_new_refresh_has_no_prior_probe_guard_or_scope_repair(history):
    add, _ = history
    add(1, task=9, signal='probe_feedback')
    add(2, task=10, status='running', signal='')
    assert not tools._turn_has_prior_time_range_guard_signal(1, 2)
    assert tools._count_probe_no_data_failures(1, 2) == 0
    with patch.object(tools, '_validate_probe_observation_provenance', return_value=None) as check:
        tools._validate_technical_repair_scope_from_history(
            turn_id=1, current_execution_id=2, current_used_datasource_ids=[], current_code='',
        )
    assert check.call_args.kwargs['previous_executions'] == []


def test_history_never_guesses_a_task_from_a_missing_execution(history):
    add, _ = history
    add(1)
    assert tools._count_probe_no_data_failures(1, 2) == 0
    assert tools._build_turn_failure_memory(1, 2)['previous_failure_messages'] == []


def test_taskless_execution_uses_run_start_without_importing_task_history(history):
    add, sessions = history
    now = datetime.now()
    with sessions() as session:
        session.add(InsightNsTurn(id=1, conversation_id=1, turn_no=1, user_query='Analyze', started_at=now))
        session.commit()
    add(1, task=None, created_at=now - timedelta(minutes=1))
    add(2, task=9, created_at=now)
    add(3, task=None, created_at=now)
    add(4, task=None, status='running', created_at=now)
    assert tools._count_probe_no_data_failures(1, 4) == 1


@pytest.mark.parametrize('signal,snapshots', [
    ('probe_feedback', {'q': query_fact(3)}),
    ('no_data_found', {}),
    ('no_data_found', {'q': query_fact(0, physical=False)}),
    ('no_data_found', {'q': query_fact(), 'other': query_fact(4)}),
    ('no_data_found', {'q': query_fact(), 'other': query_fact(0, status='failed')}),
    ('runtime_error', {'q': query_fact()}),
])
def test_retry_history_does_not_prove_global_no_data(history, signal, snapshots):
    add, _ = history
    for i in range(1, 4):
        add(i, snapshots={'old_empty': query_fact()})
    add(4, signal=signal, snapshots=snapshots)
    add(5, status='running', signal='')
    assert tools._load_confirmed_no_data_query_snapshots(turn_id=1, current_execution_id=5) == {}


def test_all_actual_empty_query_paths_are_retained(history):
    add, _ = history
    add(1, snapshots={f'q_{i}': query_fact() for i in range(15)})
    add(2, status='running', signal='')
    assert len(tools._load_confirmed_no_data_query_snapshots(turn_id=1, current_execution_id=2)) == 15


def test_observed_empty_paths_can_support_no_data_without_exhausting_retry_budget(history):
    add, _ = history
    add(1, signal='probe_feedback', snapshots={'q': query_fact()})
    add(2, status='running', signal='')
    assert tools._count_probe_no_data_failures(1, 2) == 0
    assert tools._load_confirmed_no_data_query_snapshots(turn_id=1, current_execution_id=2)


def test_technical_failure_without_queries_does_not_erase_empty_observations(history):
    add, _ = history
    add(1, signal='probe_feedback', snapshots={'q': query_fact()})
    add(2, signal='contract_error')
    add(3, status='running', signal='')
    assert tools._load_confirmed_no_data_query_snapshots(turn_id=1, current_execution_id=3)


def test_probe_with_no_data_words_is_not_a_no_data_failure(history):
    add, _ = history
    for i in range(1, 5):
        add(i, signal='probe_feedback', snapshots={'no_data_found': query_fact()})
    assert tools._count_probe_no_data_failures(1, 4) == 0
    instructions, diagnostics = tools._apply_probe_no_data_retry_limit(
        turn_id=1, current_execution_id=4, error_type='probe_feedback',
        repair_instructions=['Continue'], diagnostics={'observed': True},
    )
    assert instructions == ['Continue']
    assert diagnostics == {'observed': True}


def test_real_empty_retry_budget_does_not_force_global_no_data_or_artifacts(history):
    add, _ = history
    for i in range(1, 4):
        add(i, snapshots={'q': query_fact()})
    instructions, diagnostics = tools._apply_probe_no_data_retry_limit(
        turn_id=1, current_execution_id=3, error_type='no_data_found', repair_instructions=[],
    )
    assert diagnostics['probe_no_data_limit_reached']
    text = '\n'.join(instructions)
    assert '次数不是全部业务范围无数据的证据' in text
    assert '不要再生成 probe' not in text
    assert '至少返回一个表格' not in text


def test_observation_feedback_carries_query_facts_without_becoming_error_count():
    message = tools._tool_error_message(
        tool_call_id='probe', execution_id=7, error_type='probe_feedback',
        error_message='Fields inspected', repair_instructions=[],
        query_snapshots={'q': query_fact(2)},
    )
    payload = json.loads(message.content)
    assert payload['status'] == 'observation'
    assert payload['feedback_kind'] == 'observation'
    assert invoker._is_internal_tool_feedback(message.content)
    assert payload['diagnostics']['probe_fact_ledger']['observations'][0]['row_count'] == 2
    runtime = SimpleNamespace(analysis_task_id=10, turn=SimpleNamespace(user_query='Analyze'), short_term_memory={})
    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    assert runtime.short_term_memory['probe_feedback_count'] == 1
    assert 'probe_no_data_retry' not in runtime.short_term_memory


def test_probe_feedback_records_a_bounded_fact_ledger_for_the_same_task():
    snapshot = query_fact(2)
    snapshot['execution']['columns'] = ['batch_id', 'status']
    snapshot['implementation'] = {'datasource_identifiers': ['quality.batch']}
    payload = json.loads(tools._tool_error_message(
        tool_call_id='probe-ledger', execution_id=11, error_type='probe_feedback',
        error_message='Probe complete', repair_instructions=[], query_snapshots={'q_batch': snapshot},
    ).content)

    ledger = payload['diagnostics']['probe_fact_ledger']
    assert ledger['observations'] == [{
        'query_key': 'q_batch',
        'datasource_identifiers': ['quality.batch'],
        'query_scope': snapshot['query_scope'],
        'status': 'success',
        'row_count': 2,
        'columns': ['batch_id', 'status'],
    }]

    runtime = SimpleNamespace(analysis_task_id=10, turn=SimpleNamespace(user_query='Analyze'), short_term_memory={})
    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    assert runtime.short_term_memory['probe_fact_ledger'] == [ledger]
    payload.update(error_type='no_data_found', execution_id=8, diagnostics={
        'probe_no_data_retry_count': 1, 'probe_no_data_retry_limit': 3,
    })
    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    assert runtime.short_term_memory['probe_no_data_retry']['count'] == 1
    runtime.analysis_task_id = 11
    payload.update(error_type='probe_feedback', execution_id=9)
    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    assert 'probe_no_data_retry' not in runtime.short_term_memory


def test_recovery_feedback_does_not_duplicate_probe_ledger():
    execution = SimpleNamespace(
        id=11,
        execution_status='failed',
        result_payload_json=json.dumps({
            'retry_type': 'probe_feedback',
            'message': 'Probe complete',
            'diagnostics': {
                'probe_fact_ledger': {
                    'observations': [{'query_key': 'q_orders', 'row_count': 2}],
                },
            },
        }),
        error_message='',
    )
    full = invoker._build_structured_execution_feedback([execution])
    recovery = invoker._build_structured_execution_feedback(
        [execution], include_probe_ledger=False,
    )
    assert full[0]['probe_fact_ledger']['observations'][0]['query_key'] == 'q_orders'
    assert 'probe_fact_ledger' not in recovery[0]


def test_probe_tool_observation_keeps_react_payload_bounded_without_losing_ledger():
    snapshot = query_fact(2)
    message = tools._tool_error_message(
        tool_call_id='probe-observation',
        execution_id=11,
        error_type='probe_feedback',
        error_message='Probe complete',
        repair_instructions=[],
        diagnostics={'printed_observations': 'x' * 4000 + 'later_query_fact' + 'x' * 4000},
        query_snapshots={'q_batch': snapshot},
        current_failed_code='print("probe")',
        stdout_text='y' * 8000,
    )

    payload = json.loads(message.content)
    assert payload['status'] == 'observation'
    assert payload['diagnostics']['probe_fact_ledger']['observations']
    assert 'printed_observations' not in payload['diagnostics']
    assert 'query_snapshots' not in payload
    assert 'stdout_text' not in payload
    assert 'current_failed_code' not in payload


def test_probe_observation_preserves_custom_facts_and_all_executed_paths():
    observations = [{'field': 'status', 'values': ['open', 'closed']}]
    message = tools._tool_error_message(
        tool_call_id='probe-custom', execution_id=11,
        error_type='probe_feedback', error_message='Probe complete',
        repair_instructions=[],
        diagnostics={
            'query_observations': observations,
            'confirmed_facts': ['enum verified'],
            'printed_observations': 'summary',
            'printed_observations_truncated': True,
        },
        query_snapshots={f'path_{index}': query_fact(2) for index in range(25)},
    )
    diagnostics = json.loads(message.content)['diagnostics']
    assert diagnostics['query_observations'] == observations
    assert diagnostics['probe_fact_ledger']['confirmed_facts'] == ['enum verified']
    assert len(diagnostics['probe_fact_ledger']['observations']) == 25
    assert 'confirmed_facts' not in diagnostics
    assert 'printed_observations' not in diagnostics


def test_probe_fact_ledger_merges_helper_observation_and_exposes_capacity_fact():
    snapshot = query_fact(1)
    snapshot['implementation'] = {
        'datasource_identifiers': ['quality.results', 'process.samples'],
        'projection_semantics': [{
            'alias': 'overlap_batch_count',
            'semantic_kind': 'aggregation',
            'projection_role': 'measure',
            'expression': 'COUNT(DISTINCT batch_id)',
        }],
    }
    snapshot['execution']['columns'] = ['overlap_batch_count']
    snapshot['execution']['row_count'] = 1
    diagnostics = {
        'query_observations': [{
            'query_key': 'probe_overlap',
            'datasource_identifiers': ['quality.results', 'process.samples'],
            'status': 'success',
            'row_count': 1,
            'columns': ['overlap_batch_count'],
            'value_samples': [{'overlap_batch_count': 18825}],
        }],
    }

    ledger = tools._build_probe_fact_ledger(
        query_snapshots={'query_auto_overlap': snapshot},
        diagnostics=diagnostics,
    )

    assert len(ledger['observations']) == 1
    observation = ledger['observations'][0]
    assert observation['value_samples'] == [{'overlap_batch_count': 18825}]
    assert observation['aggregate_facts'] == [{
        'field': 'overlap_batch_count',
        'expression': 'COUNT(DISTINCT batch_id)',
        'value': 18825,
    }]
    assert observation['capacity_risks'] == [{
        'observed_population_count': 18825,
        'count_field': 'overlap_batch_count',
        'max_rows_per_physical_query': 10000,
        'required_strategy': 'query_side_final_aggregation_or_stable_pagination',
    }]


def test_probe_observation_can_carry_a_phase_specific_formal_work_packet():
    message = tools._tool_error_message(
        tool_call_id='probe-formal-packet', execution_id=12,
        error_type='probe_feedback', error_message='Probe complete',
        repair_instructions=[], formal_program_work_packet={
            'phase': 'formal_program',
            'contract_ids': ['query.local_file', 'result.artifacts'],
            'contracts': [{'contract_id': 'query.local_file', 'content': 'full details'}],
        },
    )
    payload = json.loads(message.content)
    packet = payload['diagnostics']['formal_program_work_packet']
    assert packet['phase'] == 'formal_program'
    assert packet['contract_ids'] == ['query.local_file', 'result.artifacts']


def test_probe_feedback_snapshot_compaction_keeps_scope_but_omits_replayed_sql():
    snapshot = query_fact(2)
    snapshot['implementation'] = {
        'sql': 'SELECT category, COUNT(*) AS row_count FROM demo GROUP BY category',
        'sql_hash': 'query-hash',
        'projection_semantics': [{'alias': 'row_count', 'projection_role': 'measure'}],
        'datasource_identifiers': ['demo'],
        'filter_expressions': ['status = active'],
    }

    compact = tools._compact_probe_query_snapshots_for_feedback({'q': snapshot})

    assert compact['q']['execution']['row_count'] == 2
    assert compact['q']['implementation']['sql_hash'] == 'query-hash'
    assert compact['q']['implementation']['datasource_identifiers'] == ['demo']
    assert 'sql' not in compact['q']['implementation']
    assert 'projection_semantics' not in compact['q']['implementation']


@pytest.mark.parametrize('boundary', ['snapshot', 'helper', 'outcome'])
def test_failed_probe_error_survives_worker_and_working_view_projection(boundary):
    from langchain_core.messages import AIMessage
    from agent.runtime_working_context import collect_runtime_facts, resolve_work

    failure = {
        'query_key': 'failed_query', 'status': 'query_error',
        'error_type': 'runtime_error',
        'error_message': 'invalid sql:syntax error at or near ","',
    }
    snapshots = {}
    diagnostics = {}
    if boundary == 'snapshot':
        snapshots = {'failed_query': {
            'query_key': 'failed_query',
            'implementation': {'sql': 'SELECT `category`, COUNT(*) FROM demo'},
            'execution': failure,
        }}
    else:
        diagnostics['query_observations' if boundary == 'helper'
                    else '_query_execution_outcomes'] = [failure]
    message = tools._tool_error_message(
        tool_call_id='failed-probe', execution_id=11,
        error_type='probe_feedback', error_message='Probe completed',
        repair_instructions=[], diagnostics=diagnostics, query_snapshots=snapshots,
    )
    action = AIMessage(content='', tool_calls=[{
        'id': 'failed-probe', 'name': 'probe_analysis_data',
        'args': {'code': 'SELECT `category`, COUNT(*) FROM demo'},
        'type': 'tool_call',
    }])
    messages = [action, message]
    assert resolve_work(messages)[0] == 'probe_repair'
    _, _, probe_facts = collect_runtime_facts(messages)
    observations = next(iter(probe_facts.values()))['observations']
    assert observations[0]['error_message'] == failure['error_message']


def test_implicit_probe_feedback_preserves_worker_outcomes_without_replaying_stdout():
    snapshot = query_fact(2)
    snapshot['implementation'] = {'datasource_identifiers': ['demo.contracts']}
    snapshot['execution']['columns'] = ['status', 'row_count']

    retry = tools._build_implicit_probe_feedback(
        {'q': snapshot},
        "[{'status': 'active', 'row_count': 2}]",
    )

    assert retry.retry_type == 'probe_feedback'
    assert retry.diagnostics['_query_execution_outcomes'][0]['row_count'] == 2
    assert retry.diagnostics['_query_execution_outcomes'][0]['columns'] == ['status', 'row_count']
    assert 'printed_observations' not in retry.diagnostics


def test_implicit_probe_feedback_marks_only_all_unavailable_paths_as_terminal():
    snapshot = query_fact(0)
    snapshot['implementation'] = {'datasource_identifiers': ['demo.contracts']}
    snapshot['execution'].update({
        'status': 'failed',
        'error_type': 'data_source_unavailable',
        'error_message': 'failed to find attached database',
    })

    retry = tools._build_implicit_probe_feedback(
        {'q': snapshot},
        '',
        selected_datasource_snapshot=[{
            'datasource_id': 1,
            'datasource_identifier': 'demo.contracts',
        }],
    )

    assert retry.diagnostics['_query_execution_outcomes'][0]['error_type'] == 'data_source_unavailable'
    assert retry.diagnostics['_terminal_probe_failure']['kind'] == 'all_required_query_paths_unavailable'
    assert retry.diagnostics['_terminal_probe_failure']['required_datasource_ids'] == [1]


def test_implicit_probe_feedback_normalizes_worker_query_error_for_missing_timeseries_registration():
    snapshot = query_fact(0)
    snapshot['implementation'] = {'datasource_identifiers': ['_test01_abc']}
    snapshot['execution'].update({
        'status': 'query_error',
        'error_type': 'runtime_error',
        'error_message': (
            'Binder Error: Failed to find attached database "uns_timescale" '
            'referenced in postgres_query'
        ),
    })

    retry = tools._build_implicit_probe_feedback(
        {'q': snapshot},
        '',
        selected_datasource_snapshot=[{
            'datasource_id': 1,
            'datasource_identifier': '_test01_abc',
        }],
    )

    outcome = retry.diagnostics['_query_execution_outcomes'][0]
    assert outcome['status'] == 'failed'
    assert outcome['error_type'] == 'data_source_unavailable'
    assert retry.diagnostics['_terminal_probe_failure']['kind'] == 'all_required_query_paths_unavailable'


def test_implicit_probe_feedback_keeps_multi_source_analysis_open_when_only_one_path_failed():
    snapshot = query_fact(0)
    snapshot['implementation'] = {'datasource_identifiers': ['demo.contracts']}
    snapshot['execution'].update({
        'status': 'failed',
        'error_type': 'data_source_unavailable',
        'error_message': 'failed to find attached database',
    })

    retry = tools._build_implicit_probe_feedback(
        {'q': snapshot},
        '',
        selected_datasource_snapshot=[
            {'datasource_id': 1, 'datasource_identifier': 'demo.contracts'},
            {'datasource_id': 2, 'datasource_identifier': 'demo.customers'},
        ],
    )

    assert '_terminal_probe_failure' not in retry.diagnostics


def test_formal_scanner_capability_failure_is_terminal_for_single_selected_source():
    error_message = 'invalid sql: data source "fqe_uns" does not support scanner metadata sync'

    assert tools._classify_execution_error(None, error_message) == 'data_source_unavailable'
    terminal = tools._formal_datasource_unavailable_terminal(
        error_type='data_source_unavailable',
        error_message=error_message,
        selected_datasource_snapshot=[{
            'datasource_id': 41,
            'datasource_identifier': 'some_uns_alias',
        }],
    )

    assert terminal is not None
    assert terminal['kind'] == 'all_required_query_paths_unavailable'
    assert terminal['required_datasource_ids'] == [41]
    assert terminal['source'] == 'formal_execution'


def test_formal_scanner_capability_failure_does_not_terminate_partial_multi_source_analysis():
    terminal = tools._formal_datasource_unavailable_terminal(
        error_type='data_source_unavailable',
        error_message='invalid sql: data source "fqe_uns" does not support scanner metadata sync',
        selected_datasource_snapshot=[
            {'datasource_id': 41, 'datasource_identifier': 'fqe_uns'},
            {'datasource_id': 42, 'datasource_identifier': 'customer_dimension'},
        ],
    )

    assert terminal is None


def test_formal_binder_error_remains_repairable_when_unavailability_is_not_proven():
    terminal = tools._formal_datasource_unavailable_terminal(
        error_type='data_source_unavailable',
        error_message='Binder Error: referenced column "device_name" not found',
        selected_datasource_snapshot=[{
            'datasource_id': 41,
            'datasource_identifier': 'fqe_uns',
        }],
    )

    assert terminal is None


@pytest.mark.parametrize('mode,output', [('same_turn', {'type': 'standard'}), ('disabled', {'type': 'text'}), ('disabled', {'type': 'json', 'schema': {'type': 'object'}})])
def test_backup_recovery_obeys_invocation_policy_without_inventing_filter(mode, output):
    execution = SimpleNamespace(id=5, execution_status='failed', error_message='Probe complete',
        result_payload_json=json.dumps({'error_type': 'probe_feedback', 'diagnostics': {'material_candidates': ['one']}}))
    runtime = SimpleNamespace(clarification_mode=mode, output_contract=output)
    with patch.object(invoker, '_load_turn_executions_fresh', return_value=[execution]), patch.object(invoker, '_get_latest_failed_repair_target', return_value=None):
        text = invoker._build_analysis_recovery_instruction(None, runtime, 1)
    assert json.dumps(output, ensure_ascii=False) in text
    assert ('request_analysis_clarification' in text) == (mode == 'same_turn')
    assert '单个精确筛选' not in text
    assert '禁止继续使用 LIKE' not in text
    assert '只有成功生成完整的结构化图表' not in text
    assert 'observation' in text
