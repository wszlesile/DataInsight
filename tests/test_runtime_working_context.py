import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.context_engineering_runtime import CustomContext
from agent.runtime_working_context import (
    WorkingMemoryKind,
    build_context_memory_items,
    build_short_term_memory_catalog,
    build_working_memory_index,
    build_working_request,
    collect_runtime_facts,
    merge_runtime_probe_facts,
    project_history,
    resolve_effective_work,
    resolve_runtime_material_plan,
    resolve_work,
    RuntimeMaterialPlanInput,
    RuntimeProbeRequestInput,
    RuntimeMaterialUnit,
    RuntimeWorkInput,
    plan_runtime_materials,
    request_runtime_probe,
    work_contracts,
)


def call(name, identity, **args):
    return AIMessage(content='Keep the analytical comparison.', tool_calls=[{
        'id': identity, 'name': name, 'args': args, 'type': 'tool_call',
    }])


def answer(identity, **payload):
    return ToolMessage(content=json.dumps(payload), tool_call_id=identity)


def test_work_tool_normalizes_json_containers_through_its_real_invocation():
    from agent.runtime_working_context import select_runtime_work
    args = RuntimeWorkInput.model_validate({
        'work': 'plan', 'focus': 'Compare amounts',
        'datasource_ids': '[11, 12]',
        'field_names_by_datasource': '{"11": ["amount"], "12": ["name"]}',
    })
    result = select_runtime_work.func(
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        **args.model_dump(),
    )
    assert result['datasource_ids'] == [11, 12]
    assert result['field_names_by_datasource'] == {'11': ['amount'], '12': ['name']}
    import pytest
    from pydantic import ValidationError
    for value in ('not json', 'null', '{"wrong": 11}'):
        with pytest.raises(ValidationError):
            RuntimeWorkInput.model_validate({'work': 'plan', 'focus': 'Compare', 'datasource_ids': value})


def test_initial_work_selector_cannot_enter_an_execution_state():
    import pytest
    from pydantic import ValidationError

    for work in ('probe', 'implement'):
        with pytest.raises(ValidationError):
            RuntimeWorkInput.model_validate({'work': work, 'focus': 'Bypass material planning'})


def test_probe_request_has_one_explicit_transition_and_normalizes_scope():
    args = RuntimeProbeRequestInput.model_validate({
        'focus': 'Verify the stored status values',
        'datasource_ids': '[11]',
        'field_names_by_datasource': '{"11": ["status"]}',
    })
    result = request_runtime_probe.func(
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        **args.model_dump(),
    )

    assert result == {
        'tool': 'request_runtime_probe',
        'progress_summary': '',
        'status': 'success',
        'work': 'probe',
        'focus': 'Verify the stored status values',
        'datasource_ids': [11],
        'field_names_by_datasource': {'11': ['status']},
    }


def test_work_scope_has_no_unrelated_datasource_count_limit():
    from agent.runtime_working_context import select_runtime_work
    ids = list(range(1, 31))
    result = select_runtime_work.func(
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        work='plan', focus='Connect the prepared business facts', datasource_ids=ids,
    )
    assert result['datasource_ids'] == ids


def test_work_tool_uses_current_question_as_focus_when_model_omits_it():
    from agent.runtime_working_context import select_runtime_work

    result = select_runtime_work.func(
        runtime=SimpleNamespace(context=CustomContext(
            username='test', user_query='分析设备运行趋势',
        )),
        work='plan',
    )

    assert result['status'] == 'success'
    assert result['focus'] == '分析设备运行趋势'


def test_project_history_keeps_only_latest_control_revision():
    messages = [
        call('select_runtime_work', 'old-select', work='probe', focus='Old probe'),
        answer('old-select', tool='select_runtime_work', status='success', work='probe', focus='Old probe'),
        call('select_runtime_work', 'new-select', work='implement', focus='Current implementation'),
        answer('new-select', tool='select_runtime_work', status='success', work='implement', focus='Current implementation'),
    ]

    projected, archived = project_history(messages, work='implement')

    assert archived == 1
    assert all(not (
        isinstance(message, ToolMessage) and message.tool_call_id == 'old-select'
    ) for message in projected)
    calls = [
        tool_call['id']
        for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ]
    assert calls == ['new-select']


def test_tool_call_reasoning_is_not_replayed_beside_canonical_action():
    messages = [
        AIMessage(content='A long exploratory explanation already captured by the action.', tool_calls=[{
            'id': 'plan', 'name': 'plan_runtime_materials', 'args': {
                'focus': 'Analyze orders', 'material_units': [], 'probe_required': False,
            }, 'type': 'tool_call',
        }]),
        answer('plan', tool='plan_runtime_materials', status='success',
               focus='Analyze orders', material_units=[], probe_required=False),
    ]

    projected, _ = project_history(messages, work='implement')

    assert projected[0].content == ''
    assert projected[0].tool_calls[0]['args']['focus'] == 'Analyze orders'
    assert json.loads(projected[1].content)['focus'] == 'Analyze orders'


def test_repair_references_only_exact_replayed_source_and_retains_diagnostics():
    source = 'value = query()\nresult = value.sum()'
    context = {'line_no': 2, 'error_line': 'result = value.sum()',
               'traceback_excerpt': 'TypeError: invalid value',
               'context_lines': [{'line_no': 1, 'code': 'value = query()'},
                                 {'line_no': 2, 'code': 'different source'}],
               'suggested_patch_ranges': [{'start_line': 1, 'end_line': 2, 'source': source}],
               'related_contexts': [{'location_domain': 'sql', 'context_lines': [
                   {'line_no': 1, 'code': 'value = query()'}]}]}
    messages = [call('submit_analysis_program', 'p', code=source),
                answer('p', status='failed', code_error_context=context)]
    original = copy.deepcopy(messages)
    projected, _ = project_history(messages, work='repair')
    assert projected[0].tool_calls[0]['args']['code'] == source
    actual = json.loads(projected[1].content)['code_error_context']
    assert actual['context_lines'][0] == {'line_no': 1, 'source_ref': {'tool_call_id': 'p', 'line_no': 1}}
    assert actual['context_lines'][1] == context['context_lines'][1]
    assert actual['suggested_patch_ranges'][0]['source_ref'] == {
        'tool_call_id': 'p', 'start_line': 1, 'end_line': 2}
    assert actual['traceback_excerpt'] == context['traceback_excerpt']
    assert actual['related_contexts'] == context['related_contexts']
    assert messages == original


def test_local_failures_drive_repair_and_successful_probe_continues_to_implementation():
    messages = [call('select_runtime_work', 's', work='implement', focus='Full report'),
                answer('s', tool='select_runtime_work', status='success', work='implement', focus='Full report'),
                call('submit_analysis_program', 'a', code='v1'),
                answer('a', status='failed', error_type='runtime_error')]
    assert resolve_work(messages)[:2] == ('repair', 'Full report')
    messages.extend([call('probe_analysis_data', 'p', code='probe'),
                     answer('p', error_type='probe_feedback', status='observation',
                            diagnostics={'probe_fact_ledger': {'observations': []}})])
    assert resolve_work(messages)[0] == 'repair'


def test_completed_probe_resumes_implementation_with_new_dependency_path_available():
    context = CustomContext(username='test')
    messages = [
        call('probe_analysis_data', 'probe', code='probe real enum values'),
        answer('probe', tool='execute_python', status='observation', error_type='probe_feedback',
               diagnostics={'probe_fact_ledger': {'observations': [{'query_key': 'status'}]}}),
    ]
    request = ModelRequest(
        model=None, messages=messages, runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'request_runtime_probe', 'probe_analysis_data', 'submit_analysis_program', 'execute_python',
        )],
    )

    result = build_working_request(request)

    assert '"work": "implement"' in result.system_message.content
    assert [tool.name for tool in result.tools] == ['request_runtime_probe', 'submit_analysis_program']


def test_success_probe_result_also_closes_probe_tool_surface():
    """The execution boundary may label a fact-bearing Probe as success."""
    context = CustomContext(username='test')
    messages = [
        call('select_runtime_work', 'select', work='probe', focus='Confirm enum values'),
        answer('select', tool='select_runtime_work', status='success', work='probe',
               focus='Confirm enum values'),
        call('probe_analysis_data', 'probe', code='probe real values'),
        answer('probe', tool='execute_python', status='success', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'status'}]},
        }),
    ]
    request = ModelRequest(
        model=None, messages=messages, runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'request_runtime_probe', 'probe_analysis_data', 'submit_analysis_program',
        )],
    )

    result = build_working_request(request)

    assert '"work": "implement"' in result.system_message.content
    assert [tool.name for tool in result.tools] == ['request_runtime_probe', 'submit_analysis_program']


def test_new_probe_dependency_can_be_selected_without_first_failing_formal_code():
    from agent.tools import _runtime_probe_is_closed
    from agent.runtime_working_context import resolve_work_material_scope, _resolve_prompt_material_scope

    messages = [
        answer('plan', tool='plan_runtime_materials', status='success',
               focus='Compare full scope', material_units=[
                   {'datasource_id': 11, 'field_names': ['amount', 'date']},
                   {'datasource_id': 12, 'field_names': ['name']},
               ]),
        call('probe_analysis_data', 'first', code='observe dates'),
        answer('first', status='success', diagnostics={'probe_fact_ledger': {'observations': []}}),
        call('request_runtime_probe', 'new', focus='Resolve newly discovered currency encoding'),
        answer('new', tool='request_runtime_probe', status='success', work='probe',
               focus='Resolve newly discovered currency encoding', datasource_ids=[11],
               field_names_by_datasource={'11': ['currency']}),
    ]
    runtime = SimpleNamespace(context=CustomContext(username='test'), state={'messages': messages})
    assert not _runtime_probe_is_closed(runtime)
    request = ModelRequest(model=None, messages=messages, runtime=runtime,
                           tools=[SimpleNamespace(name=n) for n in ('probe_analysis_data', 'submit_analysis_program')])
    assert [tool.name for tool in build_working_request(request).tools] == ['probe_analysis_data']
    messages.extend([
        call('probe_analysis_data', 'second', code='observe currency'),
        answer('second', status='success', diagnostics={'probe_fact_ledger': {'observations': []}}),
    ])
    assert resolve_work(messages)[:2] == ('implement', 'Compare full scope')
    expected = ([11, 12], {'11': ['amount', 'date'], '12': ['name']})
    assert resolve_work_material_scope(messages) == expected
    assert _resolve_prompt_material_scope(messages, work='implement',
        material_plan=resolve_runtime_material_plan(messages)) == expected


def test_first_implementation_sees_completed_probe_pair_until_formal_execution_takes_over():
    messages = [call('probe_analysis_data', 'probe', code='observe()'),
                answer('probe', status='success', diagnostics={'probe_fact_ledger': {'observations': []}})]
    projected, _ = project_history(messages, work='implement')
    assert projected[0].tool_calls[0]['id'] == 'probe'
    assert json.loads(projected[1].content)['status'] == 'success'
    messages.extend([call('submit_analysis_program', 'formal', code='analyze()'),
                     answer('formal', status='failed', error_type='runtime_error')])
    projected, _ = project_history(messages, work='repair')
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == 'probe' for m in projected)
    assert [c['id'] for m in projected if isinstance(m, AIMessage) for c in m.tool_calls] == ['formal']


def test_model_view_state_actions_observations_and_tools_stay_consistent_across_execution():
    messages = [HumanMessage('Compare all products'),
                call('plan_runtime_materials', 'plan', focus='Compare all products'),
                answer('plan', tool='plan_runtime_materials', status='success',
                       focus='Compare all products', probe_required=True,
                       material_units=[{'datasource_id': 1, 'field_names': ['date']}]),
                call('select_runtime_work', 'select', work='probe', focus='Confirm date format'),
                answer('select', tool='select_runtime_work', status='success',
                       work='probe', focus='Confirm date format')]
    runtime = SimpleNamespace(context=CustomContext(username='test'))
    tools = [SimpleNamespace(name=name) for name in (
        'select_runtime_work', 'request_runtime_probe', 'probe_analysis_data', 'submit_analysis_program')]

    def view(expected_work, expected_execution, latest_call=None):
        original = copy.deepcopy(messages)
        result = build_working_request(ModelRequest(
            model=None, messages=messages, runtime=runtime, tools=tools))
        assert f'"work": "{expected_work}"' in result.system_message.content
        expected_tools = {expected_execution}
        if expected_work in {'implement', 'repair'}:
            expected_tools.add('request_runtime_probe')
        assert {tool.name for tool in result.tools} == expected_tools
        calls = {c['id']: c for m in result.messages if isinstance(m, AIMessage) for c in m.tool_calls}
        results = {m.tool_call_id: m for m in result.messages if isinstance(m, ToolMessage)}
        assert calls.keys() == results.keys()
        if latest_call:
            assert next(reversed(calls)) == latest_call
            assert next(reversed(results)) == latest_call
        assert messages == original
        return result

    view('probe', 'probe_analysis_data', 'select')
    messages.extend([call('probe_analysis_data', 'probe', code='observe()'),
                     answer('probe', status='success', diagnostics={
                         'probe_fact_ledger': {'observations': []}})])
    first = view('implement', 'submit_analysis_program', 'probe')
    assert json.loads(first.messages[-1].content)['status'] == 'success'
    for number in range(2):
        identity = f'formal-{number}'
        messages.extend([call('submit_analysis_program', identity, code=f'analyze({number})'),
                         answer(identity, status='failed', error_type='runtime_error')])
        repaired = view('repair', 'submit_analysis_program', identity)
        assert [c['args']['code'] for m in repaired.messages if isinstance(m, AIMessage)
                for c in m.tool_calls if c['name'] == 'submit_analysis_program'] == [f'analyze({number})']


def test_stale_formal_probe_intent_does_not_reopen_probe_after_observation():
    """A conflicting code intent must not override the active tool phase."""
    messages = [
        call('select_runtime_work', 'select', work='implement', focus='Build the report'),
        answer('select', tool='select_runtime_work', status='success', work='implement',
               focus='Build the report'),
        call('probe_analysis_data', 'probe', code='probe values'),
        answer('probe', tool='execute_python', status='success', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'values'}]},
        }),
        call('submit_analysis_program', 'formal', code='execution_intent = "probe"'),
        answer('formal', status='failed', error_type='contract_error', diagnostics={
            'violated_contract': 'formal_tool_received_probe_program',
        }),
    ]
    assert resolve_work(messages)[0] == 'implement'


def test_blocked_stale_probe_call_does_not_reopen_probe_repair():
    """A gateway-emitted stale Probe call is a boundary error, not a new Probe."""
    messages = [
        call('select_runtime_work', 'select', work='implement', focus='Build the report'),
        answer('select', tool='select_runtime_work', status='success', work='implement',
               focus='Build the report'),
        call('probe_analysis_data', 'probe', code='repeat the same probe'),
        answer('probe', tool='execute_python', status='success', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'values'}]},
        }),
        call('probe_analysis_data', 'stale', code='repeat the same probe'),
        answer('stale', status='failed', error_type='duplicate_probe_request',
               diagnostics={'violated_contract': 'duplicate_probe_request'}),
    ]
    assert resolve_work(messages)[0] == 'implement'


def test_stale_probe_call_is_rejected_at_tool_boundary():
    """A provider-emitted old Probe call must not execute a second query."""
    from agent.tools import probe_analysis_data

    messages = [
        call('select_runtime_work', 'select', work='implement', focus='Build the report'),
        answer('select', tool='select_runtime_work', status='success', work='implement',
               focus='Build the report'),
        call('probe_analysis_data', 'probe', code='probe values'),
        answer('probe', tool='execute_python', status='success', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'values'}]},
        }),
    ]
    runtime = SimpleNamespace(state={'messages': messages}, tool_call_id='stale')

    result = probe_analysis_data.func(runtime=runtime, code='probe values')
    payload = json.loads(result.content)

    assert payload['error_type'] == 'duplicate_probe_request'
    assert payload['diagnostics']['violated_contract'] == 'duplicate_probe_request'
    assert resolve_work([
        *messages, call('probe_analysis_data', 'stale', code='probe values'), result,
    ])[:2] == ('implement', 'Build the report')

    memory = build_working_memory_index([
        *messages, call('probe_analysis_data', 'stale', code='probe values'), result,
    ])
    assert len(memory.of_kind(WorkingMemoryKind.PROBE_OBSERVATION)) == 1
    probe_failures = [
        item for item in memory.of_kind(WorkingMemoryKind.EXECUTION_FAILURE)
        if item.scope == ('probe',)
    ]
    assert len(probe_failures) == 1
    assert probe_failures[0].source_event_id == 'stale'


def test_distinct_probe_can_extend_a_completed_probe_observation():
    """A new technical unknown may be probed without discarding prior facts."""
    from agent.tools import probe_analysis_data

    messages = [
        call('probe_analysis_data', 'probe', code='observe_dates()'),
        answer('probe', status='observation', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'dates'}]},
        }),
    ]
    runtime = SimpleNamespace(state={'messages': messages}, tool_call_id='new-probe')

    with patch('agent.tools.execute_python.func', return_value='executed') as execute:
        result = probe_analysis_data.func(runtime=runtime, code='observe_categories()')

    assert result == 'executed'
    execute.assert_called_once()


def test_distinct_probe_can_extend_a_completed_probe_observation():
    """A new technical unknown may be probed without discarding prior facts."""
    from agent.tools import probe_analysis_data

    messages = [
        call('probe_analysis_data', 'probe', code='observe_dates()'),
        answer('probe', status='observation', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'dates'}]},
        }),
    ]
    runtime = SimpleNamespace(state={'messages': messages}, tool_call_id='new-probe')

    with patch('agent.tools.execute_python.func', return_value='executed') as execute:
        result = probe_analysis_data.func(runtime=runtime, code='observe_categories()')

    assert result == 'executed'
    execute.assert_called_once()


def test_structured_tool_dict_content_is_visible_to_runtime_state_machine():
    context = CustomContext(username='test')
    messages = [
        call('plan_runtime_materials', 'plan', focus='Use exact fields', material_units=[{
            'datasource_id': 12, 'field_names': ['amount'], 'purpose': 'Use the amount fact.',
        }], probe_required=False),
        answer('plan', tool='plan_runtime_materials', status='success', focus='Use exact fields',
               material_units=[{'datasource_id': 12, 'field_names': ['amount'],
                                'purpose': 'Use the amount fact.'}], probe_required=False),
        call('select_runtime_work', 'select', work='implement', focus='Use exact fields'),
        answer('select', tool='select_runtime_work', status='success', work='implement',
               focus='Use exact fields'),
        call('read_runtime_datasource_context', 'schema', datasource_ids=[12]),
        ToolMessage(
            content=json.dumps({'tool': 'read_runtime_datasource_context', 'status': 'success',
                                'datasources': [{'datasource_id': 12, 'metadata_schema': {}}]}),
            tool_call_id='schema',
        ),
    ]
    request = ModelRequest(
        model=None, messages=messages, runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'read_runtime_datasource_context', 'submit_analysis_program',
        )],
    )

    result = build_working_request(request)

    # An empty Schema result does not satisfy the planned amount dependency.
    assert [tool.name for tool in result.tools] == ['read_runtime_datasource_context', 'submit_analysis_program']


def test_planned_implementation_can_submit_directly_or_resolve_a_new_dependency():
    context = CustomContext(username='test')
    messages = [
        call('plan_runtime_materials', 'plan', focus='Compute March product performance', material_units=[{
            'datasource_id': 12, 'field_names': ['amount'], 'purpose': 'Provide the current amount fact.',
        }], probe_required=False),
        answer('plan', tool='plan_runtime_materials', status='success', focus='Compute March product performance',
               material_units=[{'datasource_id': 12, 'field_names': ['amount'],
                                'purpose': 'Provide the current amount fact.'}], probe_required=False),
    ]
    request = ModelRequest(
        model=None, messages=messages, runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'request_runtime_probe', 'probe_analysis_data', 'submit_analysis_program',
            'read_runtime_datasource_context',
        )],
    )

    result = build_working_request(request)

    assert '"work": "implement"' in result.system_message.content
    assert [tool.name for tool in result.tools] == [
        'request_runtime_probe', 'submit_analysis_program', 'read_runtime_datasource_context',
    ]


def test_probe_failure_and_retry_resume_original_repair_with_current_program():
    messages = [
        HumanMessage('Explain the whole business comparison.'),
        call('submit_analysis_program', 'code', code='current_complete_program'),
        answer('code', status='failed', error_type='query_error', error='Unknown date representation'),
        call('select_runtime_work', 'dependency', work='probe', focus='Inspect date representation'),
        answer('dependency', tool='select_runtime_work', status='success',
               work='probe', focus='Inspect date representation'),
        call('probe_analysis_data', 'bad_probe', code='probe_v1'),
        answer('bad_probe', status='failed', error_type='runtime_error'),
    ]
    assert resolve_work(messages)[:2] == ('probe_repair', 'Inspect date representation')
    messages.extend([
        call('probe_analysis_data', 'good_probe', code='probe_v2'),
        answer('good_probe', status='observation', diagnostics={'probe_fact_ledger': {
            'observations': [{'query_key': 'date_format', 'columns': ['date']}],
        }}),
    ])
    saved = copy.deepcopy(messages)
    request = ModelRequest(model=None, messages=messages,
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        tools=[SimpleNamespace(name=n) for n in (
            'select_runtime_work', 'probe_analysis_data', 'submit_analysis_program',
            'read_runtime_datasource_context',
        )])
    result = build_working_request(request)
    assert resolve_work(messages)[0] == 'repair'
    assert '"work": "repair"' in result.system_message.content
    assert 'execution.implementation' in result.system_message.content
    assert 'date_format' in result.system_message.content
    assert any(isinstance(m, AIMessage) and any(
        c['args'].get('code') == 'current_complete_program' for c in m.tool_calls
    ) for m in result.messages)
    assert 'submit_analysis_program' in [t.name for t in result.tools]
    assert messages == saved


def test_planned_probe_returns_to_parent_focus_not_probe_purpose():
    messages = [
        answer('plan', tool='plan_runtime_materials', status='success',
               focus='Compare all products', probe_required=True, probe_focus='Inspect units'),
        call('probe_analysis_data', 'probe', code='inspect_units'),
        answer('probe', status='observation', diagnostics={'probe_fact_ledger': {'observations': []}}),
    ]
    assert resolve_work(messages)[:2] == ('implement', 'Compare all products')


def test_implementation_schema_read_preserves_focus_and_delivers_exact_new_dependency():
    from agent.runtime_workspace import read_runtime_datasource_context

    source = {
        'datasource_id': 12, 'datasource_name': 'orders',
        'datasource_type': 'table', 'datasource_identifier': 'dw.orders',
        'metadata_schema': {'properties': {
            'amount': {'type': 'number'},
            'currency': {'type': 'string', 'description': 'ISO billing currency, not display currency.'},
        }},
    }
    snapshot = {'selected_datasource_ids': [12], 'selected_datasource_snapshot': [source]}
    context = CustomContext(username='test', active_datasource_snapshot=snapshot,
                            execution_datasource_snapshot=copy.deepcopy(snapshot))
    runtime = SimpleNamespace(context=context)
    messages = [
        HumanMessage('Compare orders and explain limitations.'),
        call('plan_runtime_materials', 'plan', focus='Compare order amounts',
             material_units=[{'datasource_id': 12, 'field_names': ['amount']}]),
        answer('plan', tool='plan_runtime_materials', status='success',
               focus='Compare order amounts', probe_required=False,
               material_units=[{'datasource_id': 12, 'field_names': ['amount']}]),
    ]
    tools = [SimpleNamespace(name=name) for name in (
        'submit_analysis_program', 'read_runtime_datasource_context',
    )]
    before = build_working_request(ModelRequest(
        model=None, messages=messages, runtime=runtime, tools=tools,
    ))
    assert 'read_runtime_datasource_context' in [tool.name for tool in before.tools]
    payload = read_runtime_datasource_context.func(
        runtime=runtime, datasource_ids=[12], field_names_by_datasource={'12': ['currency']},
    )
    messages.extend([
        call('read_runtime_datasource_context', 'read', datasource_ids=[12],
             field_names_by_datasource={'12': ['currency']}),
        answer('read', **payload),
    ])
    original = copy.deepcopy(messages)
    after = build_working_request(ModelRequest(
        model=None, messages=messages, runtime=runtime, tools=tools,
    ))
    assert resolve_work(messages)[:2] == ('implement', 'Compare order amounts')
    assert '"work": "implement"' in after.system_message.content
    delivered = next(message for message in after.messages
                     if isinstance(message, ToolMessage) and message.tool_call_id == 'read')
    detail = json.loads(delivered.content)['datasources'][0]['metadata_schema']['properties']
    assert detail == {'currency': source['metadata_schema']['properties']['currency']}
    assert any(isinstance(message, HumanMessage) and message.content == messages[0].content
               for message in after.messages)
    assert 'submit_analysis_program' in [tool.name for tool in after.tools]
    assert 'read_runtime_datasource_context' in [tool.name for tool in before.tools]
    # Only currency was read; the planned amount definition is still missing.
    assert 'read_runtime_datasource_context' in [tool.name for tool in after.tools]
    assert 'Schema 明细尚未完整加载' in after.system_message.content
    assert '当前已加载本工作声明的精确 Schema' not in after.system_message.content
    messages.extend([
        call('read_runtime_datasource_context', 'read-amount', datasource_ids=[12]),
        answer('read-amount', tool='read_runtime_datasource_context', status='success',
               datasources=[{'datasource_id': 12, 'metadata_schema': {
                   'properties': {'amount': source['metadata_schema']['properties']['amount']}}}]),
    ])
    complete = build_working_request(ModelRequest(model=None, messages=messages, runtime=runtime, tools=tools))
    assert 'read_runtime_datasource_context' in [tool.name for tool in complete.tools]
    assert 'Schema 明细尚未完整加载' not in complete.system_message.content
    assert messages[:len(original)] == original


def test_implementation_view_reuses_canonical_schema_and_compacts_reader_transcript():
    source = {
        'datasource_id': 12, 'datasource_name': 'orders',
        'metadata_schema': {'properties': {
            'amount': {'type': 'number', 'description': 'Exact amount definition.'},
            'status': {'type': 'string', 'description': 'Order status.'},
        }},
    }
    snapshot = {'selected_datasource_ids': [12], 'selected_datasource_snapshot': [source]}
    context = CustomContext(
        username='test', active_datasource_snapshot=snapshot,
        execution_datasource_snapshot=copy.deepcopy(snapshot),
        runtime_schema_detail_workspace=[{
            'datasource_id': 12, 'datasource_name': 'orders',
            'metadata_schema': {'properties': {
                'amount': {'type': 'number', 'description': 'Exact amount definition.'},
            }},
        }],
    )
    messages = [
        HumanMessage('Analyze order amounts.'),
        call('plan_runtime_materials', 'plan', focus='Compute order amounts',
             material_units=[{'datasource_id': 12, 'field_names': ['amount']}]),
        answer('plan', tool='plan_runtime_materials', status='success', focus='Compute order amounts',
               material_units=[{'datasource_id': 12, 'field_names': ['amount']}]),
        call('select_runtime_work', 'implement', work='implement',
             focus='Implement order amount analysis', datasource_ids=[12],
             field_names_by_datasource={'12': ['amount']}),
        answer('implement', tool='select_runtime_work', status='success', work='implement',
               focus='Implement order amount analysis', datasource_ids=[12],
               field_names_by_datasource={'12': ['amount']}),
        call('read_runtime_datasource_context', 'read', datasource_ids=[12],
             field_names_by_datasource={'12': ['amount']}),
        answer('read', tool='read_runtime_datasource_context', status='success',
               datasources=[{'datasource_id': 12, 'metadata_schema': {
                   'properties': {'amount': {'type': 'number', 'description': 'Exact amount definition.'}},
               }}]),
    ]
    request = ModelRequest(
        model=None, messages=messages,
        runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'submit_analysis_program', 'read_runtime_datasource_context',
        )],
    )
    result = build_working_request(request)
    assert 'Exact amount definition.' in result.system_message.content
    assert 'Order status.' not in result.system_message.content
    reader_result = next(
        message for message in result.messages
        if isinstance(message, ToolMessage) and message.tool_call_id == 'read'
    )
    assert 'Exact amount definition.' not in reader_result.content
    assert 'runtime datasource_details' in reader_result.content


def test_runtime_never_exposes_the_generic_execution_worker():
    request = ModelRequest(
        model=None,
        messages=[
            call('plan_runtime_materials', 'plan', focus='Verify source coverage', material_units=[{
                'datasource_id': 12, 'purpose': 'Verify source coverage.',
            }], probe_required=True),
            answer('plan', tool='plan_runtime_materials', status='success', focus='Verify source coverage',
                   material_units=[{'datasource_id': 12, 'purpose': 'Verify source coverage.'}],
                   probe_required=True),
        ],
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        tools=[SimpleNamespace(name=name) for name in (
            'execute_python', 'probe_analysis_data', 'submit_analysis_program', 'select_runtime_work',
            'request_runtime_probe',
        )],
    )

    result = build_working_request(request)

    assert {tool.name for tool in result.tools} == {'probe_analysis_data'}


def test_failed_probe_stays_in_probe_repair_until_a_structured_observation_exists():
    messages = [
        call('probe_analysis_data', 'p', code='probe with an unsupported join'),
        answer('p', tool='execute_python', status='failed', error_type='query_constraint_violation',
               error_message='unconfirmed relationship cannot be joined'),
    ]

    assert resolve_work(messages)[0] == 'probe_repair'
    context = CustomContext(username='test')
    assert 'execution.probe' in work_contracts(context, 'probe_repair')
    assert 'result.artifacts' not in work_contracts(context, 'probe_repair')

    request = ModelRequest(
        model=None, messages=messages, runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'request_runtime_probe', 'probe_analysis_data', 'submit_analysis_program',
        )],
    )
    result = build_working_request(request)
    assert 'execution.probe' in result.system_message.content
    assert [tool.name for tool in result.tools] == ['probe_analysis_data']


def test_probe_source_sent_to_formal_tool_receives_probe_repair_context():
    messages = [
        call('submit_analysis_program', 'misrouted', code='probe_data_with_sql("SELECT 1")'),
        answer('misrouted', status='failed', error_type='contract_error', diagnostics={
            'violated_contract': 'formal_tool_received_probe_program',
        }),
    ]
    request = ModelRequest(
        model=None, messages=messages,
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'request_runtime_probe', 'probe_analysis_data', 'submit_analysis_program',
        )],
    )
    result = build_working_request(request)
    assert resolve_work(messages)[0] == 'probe_repair'
    assert 'execution.probe' in result.system_message.content
    assert 'result.artifacts' not in result.system_message.content
    assert [tool.name for tool in result.tools] == ['probe_analysis_data']


def test_history_keeps_only_latest_unresolved_probe_for_repair():
    messages = [
        call('probe_analysis_data', 'p1', code='first failed probe'),
        answer('p1', tool='execute_python', status='failed', error_type='contract_error',
               error_message='first failure'),
        call('probe_analysis_data', 'p2', code='second failed probe'),
        answer('p2', tool='execute_python', status='failed', error_type='contract_error',
               error_message='second failure'),
    ]

    projected, count = project_history(messages)

    assert count == 1
    assert [message.tool_call_id for message in projected if isinstance(message, ToolMessage)] == ['p2']
    calls = [
        tool_call['id']
        for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ]
    assert calls == ['p2']
    assert projected[-1].content == messages[-1].content


def test_material_plan_preserves_pydantic_units_from_the_tool_boundary():
    unit = RuntimeMaterialUnit(
        question_ids=['q1'], fact_ids=['f1'], datasource_id=12,
        field_names=['amount'], relationship_ids=['r1'], purpose='Compute the order amount.',
    )
    result = plan_runtime_materials.func(
        focus='Map the order amount.', material_units=[unit], probe_required=False,
    )
    assert result['material_units'] == [{
        'question_ids': ['q1'], 'fact_ids': ['f1'], 'datasource_id': 12,
        'field_names': ['amount'], 'relationship_ids': ['r1'],
        'purpose': 'Compute the order amount.',
    }]


def test_history_replaces_old_full_code_not_facts_or_tool_pairs():
    messages = [HumanMessage('All products'), call('submit_analysis_program', 'a', code='old code'),
                answer('a', error_type='runtime_error', current_failed_code='old code',
                       code_error_context={'trace': 'old stack'}, diagnostics={'observed': [1, 2]}),
                call('submit_analysis_program', 'b', code='current code'),
                answer('b', error_type='runtime_error', current_failed_code='current code',
                       code_error_context={'line': 2})]
    saved = copy.deepcopy(messages)
    projected, count = project_history(messages)
    assert count == 1
    assert messages == saved
    assert 'Superseded' in projected[1].tool_calls[0]['args']['code']
    assert projected[3].tool_calls[0]['args']['code'] == 'current code'
    assert json.loads(projected[2].content)['diagnostics'] == {'observed': [1, 2]}
    assert json.loads(projected[4].content)['code_error_context'] == {'line': 2}
    assert [m.tool_call_id for m in projected if isinstance(m, ToolMessage)] == ['a', 'b']
    assert projected[1].content == ''


def test_formal_repair_view_keeps_only_current_program_pair():
    messages = [
        call('submit_analysis_program', 'old', code='old complete program'),
        answer('old', status='failed', error_type='runtime_error',
               code_error_context={'line': 10, 'source': 'old failure'}),
        call('submit_analysis_program', 'current', code='current complete program'),
        answer('current', status='failed', error_type='runtime_error',
               code_error_context={'line': 20, 'source': 'current failure'}),
    ]

    projected, archived = project_history(messages, work='repair')

    assert archived == 1
    assert [m.tool_call_id for m in projected if isinstance(m, ToolMessage)] == ['current']
    calls = [
        tool_call['id']
        for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ]
    assert calls == ['current']
    assert 'old complete program' not in str(projected)
    assert 'current complete program' in str(projected)
    assert 'current failure' in str(projected)


def test_project_history_compacts_reader_payload_after_canonical_workspace_is_ready():
    messages = [
        call('read_runtime_datasource_context', 'read', datasource_ids=[11],
             field_names_by_datasource={'11': ['amount']}),
        answer('read', tool='read_runtime_datasource_context', status='success',
               datasources=[{'datasource_id': 11, 'metadata_schema': {
                   'properties': {'amount': {'description': 'Exact amount definition.'}},
               }}],
               selected_datasource_ids=[11]),
    ]

    projected, archived = project_history(
        messages,
        work='implement',
        compact_schema_reader_results=True,
    )

    assert archived == 0
    assert len(projected) == 2
    assert projected[0].tool_calls[0]['name'] == 'read_runtime_datasource_context'
    payload = json.loads(projected[1].content)
    assert payload['delivery'].startswith('Exact Schema details')
    assert 'Exact amount definition.' not in projected[1].content


def test_project_history_keeps_explicit_relationship_reader_details():
    messages = [
        call('read_runtime_datasource_context', 'read', datasource_ids=[11],
             include_relationships=True),
        answer('read', tool='read_runtime_datasource_context', status='success',
               datasources=[{'datasource_id': 11, 'metadata_schema': {'properties': {}}}],
               business_context={'relationships': [{
                   'relationship_id': 'r1', 'aggregation_safety': {
                       'left': {'status': 'conditional', 'reason': 'Preaggregate child rows.'},
                   },
               }]}),
    ]

    projected, _ = project_history(
        messages,
        work='implement',
        compact_schema_reader_results=True,
    )

    assert 'Preaggregate child rows.' in projected[1].content


def test_patch_base_stays_until_replaced_by_a_full_program():
    messages = [call('submit_analysis_program', 'a', code='base'),
                answer('a', status='failed'),
                call('submit_analysis_program', 'b', base_execution_id=1, patches=[{'start_line': 1}])]
    result, count = project_history(messages)
    assert count == 0
    assert result[0].tool_calls[0]['args']['code'] == 'base'


def test_repair_view_keeps_only_active_patch_derivation_chain():
    messages = [
        call('submit_analysis_program', 'base', code='base program'),
        answer('base', status='failed', execution_id=11, error_type='runtime_error'),
        call('submit_analysis_program', 'old-patch', base_execution_id=11,
             patches=[{'start_line': 1, 'end_line': 1, 'replacement': 'old'}]),
        answer('old-patch', status='failed', execution_id=12, error_type='runtime_error'),
        call('submit_analysis_program', 'current-patch', base_execution_id=11,
             patches=[{'start_line': 1, 'end_line': 1, 'replacement': 'current'}]),
        answer('current-patch', status='failed', execution_id=13, error_type='runtime_error'),
    ]

    projected, _ = project_history(messages, work='repair')

    retained = [
        tool_call['id']
        for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ]
    assert retained == ['base', 'current-patch']
    base_result = next(
        json.loads(message.content) for message in projected
        if isinstance(message, ToolMessage) and message.tool_call_id == 'base'
    )
    assert base_result == {
        'tool': 'execute_python',
        'status': 'failed',
        'execution_id': 11,
        'error_type': 'runtime_error',
        'history_status': 'program_derivation_base',
    }


def test_accepted_source_aliases_share_program_revision_lifecycle():
    messages = [
        call('submit_analysis_program', 'first', program='first complete source'),
        answer('first', status='failed'),
        call('submit_analysis_program', 'second', analysis_code='second complete source'),
        answer('second', status='failed'),
        call('submit_analysis_program', 'third', code='current complete source',
             analysis_program='ignored alias source'),
    ]
    original = copy.deepcopy(messages)
    projected, count = project_history(messages)
    assert count == 2
    assert messages == original
    assert 'Superseded' in projected[0].tool_calls[0]['args']['code']
    assert 'Superseded' in projected[2].tool_calls[0]['args']['code']
    assert projected[4].tool_calls[0]['args'] == {'code': 'current complete source'}
    assert 'first complete source' not in str(projected)
    assert 'second complete source' not in str(projected)
    assert 'ignored alias source' not in str(projected)
    assert [m.tool_call_id for m in projected if isinstance(m, ToolMessage)] == ['first', 'second']


def test_typed_memory_index_distinguishes_replacement_scope_and_query_execution():
    snapshot = {
        'query_key': 'monthly_orders',
        'query_constraints': {'month': '2026-08'},
        'execution': {'row_count': 8},
    }
    messages = [
        HumanMessage('按完整月份分析订单。'),
        call('submit_analysis_program', 'full-1', code='first complete program'),
        answer('full-1', status='failed', error_type='syntax_error'),
        call('submit_analysis_program', 'patch-1', base_execution_id=11, patches=[{'start_line': 2}]),
        answer('probe-1', tool='probe_analysis_data', query_snapshots={'q': snapshot}),
        call('submit_analysis_program', 'full-2', code='second complete program'),
        answer('mystery', payload={'unowned': 'tool result'}),
    ]
    index = build_working_memory_index(messages)
    programs = index.of_kind(WorkingMemoryKind.PROGRAM)
    assert [item.status for item in programs] == ['superseded', 'active']
    assert programs[0].superseded_by == 'full-2'
    assert index.of_kind(WorkingMemoryKind.PROGRAM_PATCH)[0].dependencies == ('execution:11',)
    assert len(index.of_kind(WorkingMemoryKind.QUERY_DEFINITION)) == 1
    assert len(index.of_kind(WorkingMemoryKind.QUERY_EXECUTION)) == 1
    assert index.of_kind(WorkingMemoryKind.UNKNOWN)


def test_context_memory_items_type_preparation_schema_and_active_contracts_without_copying_payloads():
    context = CustomContext(
        username='test',
        active_datasource_snapshot={
            'datasources': [{'datasource_id': 12, 'metadata_schema': {'properties': {'amount': {'type': 'number'}}}}],
            'analysis_semantic_context': {'business_goal': 'monthly revenue'},
            'runtime_schema_workspace': {'mode': 'indexed', 'sources': [12]},
        },
    )
    items = build_context_memory_items(context, ('context.analysis', 'execution.core'))
    assert {item.kind for item in items} >= {
        WorkingMemoryKind.PREPARATION_FACT,
        WorkingMemoryKind.SCHEMA_SOURCE,
        WorkingMemoryKind.PROTOCOL,
    }
    assert all(item.source_index == -1 for item in items)
    assert any(item.logical_key == 'schema:12' for item in items)


def test_short_term_memory_catalog_preserves_existing_values_as_typed_metadata_only():
    memory = {
        'expected_time_range_contract': {'start': '2026-08-01', 'end': '2026-09-01'},
        'probe_fact_ledger': [{'observations': [{'query_key': 'orders', 'row_count': 3}]}],
        'runtime_schema_detail_workspace': [{'datasource_id': 12, 'metadata_schema': {'properties': {}}}],
        'unmigrated_value': {'keep': True},
    }
    catalog = build_short_term_memory_catalog(memory)
    assert memory['unmigrated_value'] == {'keep': True}
    assert {item['kind'] for item in catalog} >= {
        WorkingMemoryKind.INTERACTION_CONSTRAINT.value,
        WorkingMemoryKind.PROBE_OBSERVATION.value,
        WorkingMemoryKind.SCHEMA_SOURCE.value,
        WorkingMemoryKind.UNKNOWN.value,
    }
    context = CustomContext(username='test', working_memory_catalog=catalog)
    typed = build_context_memory_items(context, ())
    assert any(item.logical_key == 'schema:detail:12' for item in typed)


def test_reference_and_probe_packets_are_not_replayed_but_observations_survive():
    messages = [answer('r', tool='read_runtime_reference', references=[{
        'contract_id': 'result.artifacts', 'sha256': 'abc', 'content': 'duplicated protocol'}]),
        call('probe_analysis_data', 'p', code='probe'),
        answer('p', diagnostics={'formal_program_work_packet': {'content': 'duplicate'},
                                 'query_observations': [{'values': ['active']}]})]
    result, _ = project_history(messages)
    assert 'duplicated protocol' not in result[0].content
    assert 'active' in result[2].content
    assert 'formal_program_work_packet' not in result[2].content


def test_probe_code_is_archived_only_when_its_query_facts_are_captured():
    observed = [
        call('probe_analysis_data', 'p1', code='query the actual status values'),
        answer('p1', tool='probe_analysis_data', query_snapshots={
            'status': {'query_key': 'status', 'execution': {'row_count': 2}},
        }),
    ]
    unobserved = [
        call('probe_analysis_data', 'p2', code='failed probe source remains needed'),
        answer('p2', tool='probe_analysis_data', error_type='query_error'),
    ]
    projected, count = project_history([*observed, *unobserved])
    assert count == 1
    assert projected[0].tool_calls == []
    assert all(
        not (isinstance(message, ToolMessage) and message.tool_call_id == 'p1')
        for message in projected
    )
    assert projected[1].tool_calls[0]['args']['code'] == 'failed probe source remains needed'


def test_latest_probe_pair_survives_until_a_newer_execution():
    messages = [
        call('probe_analysis_data', 'p1', code='probe code'),
        answer('p1', tool='probe_analysis_data', diagnostics={
            'probe_fact_ledger': {'observations': [{'query_key': 'orders', 'row_count': 5}]},
        }),
    ]
    index = build_working_memory_index(messages)
    assert index.of_kind(WorkingMemoryKind.PROBE_OBSERVATION)[0].status == 'facts_captured'
    projected, _ = project_history(messages, memory=index)
    assert projected[0].tool_calls[0]['id'] == 'p1'
    assert projected[1].tool_call_id == 'p1'
    messages.extend([
        call('submit_analysis_program', 'formal', code='complete source'),
        answer('formal', status='failed', error_type='runtime_error'),
    ])
    projected, _ = project_history(messages)
    assert projected[0].tool_calls == []
    assert [m.tool_call_id for m in projected if isinstance(m, ToolMessage)] == ['formal']


def test_latest_probe_pair_retains_action_and_links_typed_observation():
    messages = [
        call('probe_analysis_data', 'probe-1', code='print(a_very_large_dataframe)'),
        answer('probe-1', tool='execute_python', status='observation', error_type='probe_feedback', diagnostics={
            'printed_observations': 'raw-table-' * 4000,
            'probe_fact_ledger': {
                'schema_version': 1,
                'observations': [{
                    'query_key': 'status_values',
                    'status': 'success',
                    'row_count': 2,
                    'columns': ['status', 'count'],
                    'probe_profile': {
                        'sample_rows': [{'status': 'open', 'count': 8}],
                        'column_profiles': [{'field': 'status', 'sample_values': ['open', 'closed']}],
                    },
                }],
            },
        }),
    ]
    memory = build_working_memory_index(messages)
    assert memory.of_kind(WorkingMemoryKind.PROBE_OBSERVATION)[0].status == 'facts_captured'
    archived, count = project_history(messages, memory=memory)
    assert count == 0
    assert archived[0].tool_calls[0]['id'] == 'probe-1'
    assert len(archived) == 2

    projected, query_facts, probe_facts = collect_runtime_facts(messages)
    assert not query_facts
    assert len(probe_facts) == 1
    payload = json.loads(projected[1].content)
    assert payload['diagnostics']['probe_fact_refs']
    assert 'raw-table' not in projected[1].content
    fact = next(iter(probe_facts.values()))
    assert fact['observations'][0]['probe_profile']['sample_rows'][0]['status'] == 'open'


def test_probe_fact_projection_keeps_bounded_helper_value_samples():
    messages = [
        call('probe_analysis_data', 'probe-1', code='probe enum'),
        answer('probe-1', tool='execute_python', diagnostics={'probe_fact_ledger': {
            'observations': [{
                'query_key': 'probe_status', 'status': 'success', 'row_count': 2,
                'columns': ['status', 'record_count'],
                'value_samples': [
                    {'status': 'open', 'record_count': 8},
                    {'status': 'closed', 'record_count': 2},
                ],
            }],
        }}),
    ]

    _, _, facts = collect_runtime_facts(messages)

    fact = next(iter(facts.values()))
    assert fact['observations'][0]['value_samples'] == [
        {'status': 'open', 'record_count': 8},
        {'status': 'closed', 'record_count': 2},
    ]


def test_context_probe_ledger_merges_with_transcript_without_duplicate_facts():
    ledger = {
        'observations': [{'query_key': 'q_status', 'datasource_id': 12, 'row_count': 2}],
        'confirmed_facts': [{'fact_id': 'status_format', 'datasource_id': 12, 'value': 'open'}],
    }
    merged = merge_runtime_probe_facts({'transcript': ledger}, [ledger])
    fact = next(iter(merged.values()))
    assert fact['observations'] == ledger['observations']
    assert fact['confirmed_facts'] == ledger['confirmed_facts']


def test_plan_workspace_reprojects_schema_detail_read_from_typed_context():
    source = {
        'datasource_id': 12,
        'datasource_name': 'orders',
        'metadata_schema': {'properties': {
            'amount': {'type': 'number', 'description': 'Net order amount.'},
        }},
    }
    active = {
        'selected_datasource_ids': [12],
        'selected_datasource_snapshot': [source],
        'analysis_semantic_context': {'insight_lineage': [], 'relationships': []},
    }
    detail = {
        'datasource_id': 12,
        'metadata_schema': {'properties': {
            'amount': {'type': 'number', 'description': 'Net order amount.'},
        }},
    }
    request = ModelRequest(
        model=None,
        messages=[
            HumanMessage('Analyze orders'),
            call('select_runtime_work', 'select', work='plan', focus='Map order facts'),
            answer('select', tool='select_runtime_work', status='success', work='plan', focus='Map order facts'),
        ],
        runtime=SimpleNamespace(context=CustomContext(
            username='test', active_datasource_snapshot=active,
            execution_datasource_snapshot=copy.deepcopy(active),
            runtime_schema_detail_workspace=[detail],
        )),
        tools=[SimpleNamespace(name=name) for name in (
            'plan_runtime_materials', 'read_runtime_datasource_context',
        )],
    )
    result = build_working_request(request)
    assert '"loaded_field_names":["amount"]' in result.system_message.content


def test_probe_fact_ledger_merges_repeated_observations_and_rewrites_all_references():
    messages = [
        call('probe_analysis_data', 'probe-1', code='first observation'),
        answer('probe-1', tool='execute_python', diagnostics={'probe_fact_ledger': {
            'observations': [{'query_key': 'status', 'datasource_identifiers': ['orders'],
                              'query_constraints': {'target_filters': []}, 'columns': ['status'],
                              'row_count': 2}],
        }}),
        call('probe_analysis_data', 'probe-2', code='latest observation'),
        answer('probe-2', tool='execute_python', diagnostics={'probe_fact_ledger': {
            'observations': [{'query_key': 'status', 'datasource_identifiers': ['orders'],
                              'query_constraints': {'target_filters': []}, 'columns': ['status'],
                              'row_count': 3,
                              'projection_semantics': [{'alias': 'population', 'expression': 'COUNT(*)'}],
                              'aggregate_facts': [{'field': 'population', 'value': 18825}],
                              'capacity_risks': [{
                                  'observed_population_count': 18825,
                                  'max_rows_per_physical_query': 10000,
                                  'required_strategy': 'query_side_final_aggregation_or_stable_pagination',
                              }]}],
        }}),
    ]
    projected, _, facts = collect_runtime_facts(messages)
    assert len(facts) == 1
    fact_id, fact = next(iter(facts.items()))
    assert fact['observations'] == [{
        'query_key': 'status', 'datasource_identifiers': ['orders'],
        'query_constraints': {'target_filters': []}, 'columns': ['status'], 'row_count': 3,
        'projection_semantics': [{'alias': 'population', 'expression': 'COUNT(*)'}],
        'aggregate_facts': [{'field': 'population', 'value': 18825}],
        'capacity_risks': [{
            'observed_population_count': 18825,
            'max_rows_per_physical_query': 10000,
            'required_strategy': 'query_side_final_aggregation_or_stable_pagination',
        }],
    }]
    for index in (1, 3):
        assert json.loads(projected[index].content)['diagnostics']['probe_fact_refs'] == [fact_id]


def test_new_probe_material_view_replaces_local_plan_and_returns_to_parent_materials():
    messages = [
        call('plan_runtime_materials', 'plan', focus='Map order amount', material_units=[{
            'question_ids': ['q1'], 'fact_ids': ['f1'], 'datasource_id': 12,
            'field_names': ['amount'], 'relationship_ids': [], 'purpose': 'Compute order amount.',
        }]),
        answer('plan', tool='plan_runtime_materials', status='success', focus='Map order amount',
               material_units=[{
                   'question_ids': ['q1'], 'fact_ids': ['f1'], 'datasource_id': 12,
                   'field_names': ['amount'], 'relationship_ids': [], 'purpose': 'Compute order amount.',
               }], probe_required=False),
        call('select_runtime_work', 'probe', work='probe', focus='Verify values',
             datasource_ids=[1, 2, 3, 4], field_names_by_datasource={'1': ['unplanned_field']}),
        answer('probe', tool='select_runtime_work', status='success', work='probe', focus='Verify values',
               datasource_ids=[1, 2, 3, 4], field_names_by_datasource={'1': ['unplanned_field']}),
    ]
    from agent.runtime_working_context import resolve_work_material_scope
    assert resolve_work_material_scope(messages) == ([1, 2, 3, 4], {'1': ['unplanned_field']})
    messages.extend([
        call('probe_analysis_data', 'check', code='probe'),
        answer('check', status='observation', diagnostics={'probe_fact_ledger': {'observations': []}}),
    ])
    assert resolve_work(messages)[:2] == ('implement', 'Map order amount')
    assert resolve_work_material_scope(messages) == ([12], {'12': ['amount']})
    from langchain_core.messages import messages_from_dict, messages_to_dict
    restored = messages_from_dict(json.loads(json.dumps(messages_to_dict(messages))))
    assert resolve_work(restored) == resolve_work(messages)
    assert resolve_work_material_scope(restored) == resolve_work_material_scope(messages)


def test_runtime_reference_keeps_only_the_latest_work_step_reference():
    messages = [
        answer('reference-1', tool='read_runtime_reference', references=[{'contract_id': 'query.relational'}]),
        answer('reference-2', tool='read_runtime_reference', references=[{'contract_id': 'result.artifacts'}]),
    ]
    work, _, references = resolve_work(messages)
    assert work == 'understand'
    assert references == ['result.artifacts']


def test_initial_direct_implementation_is_routed_through_material_planning():
    messages = [
        call('select_runtime_work', 'select', work='implement', focus='Analyze orders',
             datasource_ids=[12], field_names_by_datasource={'12': ['amount']}),
        answer('select', tool='select_runtime_work', status='success', work='implement',
               focus='Analyze orders', datasource_ids=[12], field_names_by_datasource={'12': ['amount']}),
    ]
    assert resolve_work(messages)[:2] == ('implement', 'Analyze orders')
    assert resolve_effective_work(messages)[:2] == ('plan', 'Analyze orders')


def test_material_plan_becomes_precise_schema_scope_and_next_execution_state():
    messages = [
        call('select_runtime_work', 'select', work='plan', focus='Map order amount'),
        answer('select', tool='select_runtime_work', status='success', work='plan', focus='Map order amount'),
        call('plan_runtime_materials', 'plan', focus='Map order amount', material_units=[{
            'question_ids': ['q1'], 'fact_ids': ['f1'], 'datasource_id': 12,
            'field_names': ['amount'], 'relationship_ids': [], 'purpose': 'Compute order amount.',
        }]),
        answer('plan', tool='plan_runtime_materials', status='success', focus='Map order amount',
               material_units=[{
                   'question_ids': ['q1'], 'fact_ids': ['f1'], 'datasource_id': 12,
                   'field_names': ['amount'], 'relationship_ids': [], 'purpose': 'Compute order amount.',
               }], probe_required=False),
    ]
    assert resolve_work(messages)[:2] == ('implement', 'Map order amount')
    assert resolve_runtime_material_plan(messages)['material_units'][0]['field_names'] == ['amount']


def test_material_plan_accepts_a_wide_source_without_an_arbitrary_field_cap():
    fields = [f'field_{index}' for index in range(21)]
    plan = RuntimeMaterialPlanInput(
        focus='Inspect the complete source profile',
        material_units=[{
            'datasource_id': 12,
            'field_names': fields,
            'purpose': 'Keep all source fields available for the focused read.',
        }],
    )
    assert plan.material_units[0].field_names == fields


def test_planned_implementation_keeps_schema_compact_until_an_exact_read():
    source = {
        'datasource_id': 12,
        'datasource_name': 'orders',
        'metadata_schema': {'properties': {
            'amount': {'type': 'number', 'description': 'Net order amount.'},
            'currency': {'type': 'string', 'description': 'Billing currency.'},
        }},
    }
    snapshot = {
        'selected_datasource_ids': [12],
        'selected_datasource_snapshot': [source],
        'analysis_semantic_context': {
            'insight_lineage': [{'fact_bindings': [
                {'datasource_id': 12, 'fields': ['amount', 'currency']},
            ]}],
        },
    }
    messages = [
        call('plan_runtime_materials', 'plan', focus='Compare order amounts', material_units=[{
            'datasource_id': 12, 'field_names': ['amount', 'currency'],
            'purpose': 'Provide order facts.',
        }]),
        answer('plan', tool='plan_runtime_materials', status='success',
               focus='Compare order amounts', probe_required=False,
               material_units=[{
                   'datasource_id': 12, 'field_names': ['amount', 'currency'],
                   'purpose': 'Provide order facts.',
               }]),
    ]
    context = CustomContext(
        username='test', active_datasource_snapshot=snapshot,
        execution_datasource_snapshot=copy.deepcopy(snapshot),
    )
    request = ModelRequest(
        model=None,
        messages=messages,
        runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name=name) for name in (
            'read_runtime_datasource_context', 'submit_analysis_program',
        )],
    )
    result = build_working_request(request)
    assert '当前材料计划的 Schema 明细尚未完整加载' in result.system_message.content
    assert 'billing currency' not in result.system_message.content
    assert 'read_runtime_datasource_context' in [tool.name for tool in result.tools]


def test_initial_direct_implementation_compiles_a_plan_view_without_mutating_history():
    context = CustomContext(username='test')
    messages = [SystemMessage('Rules\n<runtime-contract-summaries>\nold packs\n</runtime-contract-summaries>'),
                HumanMessage('Analyze'),
                answer('s', tool='select_runtime_work', status='success', work='implement', focus='Report')]
    request = ModelRequest(model=None, messages=messages, runtime=SimpleNamespace(context=context),
                           tools=[SimpleNamespace(name=n) for n in (
                               'select_runtime_work', 'plan_runtime_materials',
                               'probe_analysis_data', 'submit_analysis_program',
                           )])
    result = build_working_request(request)
    assert 'execution.planning' in result.system_message.content
    assert 'build_chart_result' not in result.system_message.content
    assert 'old packs' not in result.system_message.content
    assert 'old packs' in request.messages[0].content
    assert [t.name for t in result.tools] == ['plan_runtime_materials']
    assert '"work": "plan"' in result.system_message.content


def test_request_compiler_replaces_nested_prior_work_views_instead_of_accumulating_them():
    context = CustomContext(username='test')
    messages = [SystemMessage(
        'Base\n<runtime-contract-summaries>\nold packs\n</runtime-contract-summaries>\n'
        '<runtime-working-context>\nold focused pack\n</runtime-working-context>'
    ), HumanMessage('Analyze')]
    request = ModelRequest(
        model=None, messages=messages, runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name='select_runtime_work')],
    )
    result = build_working_request(request)
    assert result.system_message.content.count('<runtime-working-context>') == 1
    assert 'old packs' not in result.system_message.content
    assert 'old focused pack' not in result.system_message.content
    assert result.system_message.content.count('Base') == 1


def test_request_compiler_removes_initial_full_contract_packet_before_current_work_loads():
    context = CustomContext(username='test')
    request = ModelRequest(
        model=None,
        messages=[SystemMessage(
            'Base\n<runtime-contracts>\ninitial full execution protocol\n</runtime-contracts>\n'
            '<runtime-contract-summaries>\ninitial summaries\n</runtime-contract-summaries>'
        ), HumanMessage('Analyze')],
        runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name='select_runtime_work')],
    )

    result = build_working_request(request)

    assert 'initial full execution protocol' not in result.system_message.content
    assert 'initial summaries' not in result.system_message.content
    assert 'Base' in result.system_message.content


def test_request_compiler_keeps_literal_tag_mentions_in_semantic_prompt_prose():
    context = CustomContext(username='test')
    literal = 'The phrase `<runtime-working-context>` describes request state.'
    request = ModelRequest(
        model=None,
        messages=[SystemMessage(literal), HumanMessage('Analyze')],
        runtime=SimpleNamespace(context=context),
        tools=[SimpleNamespace(name='select_runtime_work')],
    )
    result = build_working_request(request)
    assert literal in result.system_message.content


def test_mixed_source_capability_union_and_external_output_mode():
    from unittest.mock import patch
    context = CustomContext(username='test', output_contract={'type': 'json', 'schema': {'type': 'object'}},
                            clarification_mode='disabled')
    with patch('agent.runtime_working_context.infer_query_capabilities_from_snapshot', return_value=('sql', 'time_series', 'local_file')):
        ids = work_contracts(context, 'repair')
    assert {'query.relational', 'query.time_series', 'query.local_file', 'query.cross_source',
            'execution.implementation', 'result.json', 'interaction.no_clarification'}.issubset(ids)
    assert 'result.artifacts' not in ids


def test_followup_work_keeps_the_followup_context_contract_in_every_work_mode():
    context = CustomContext(username='test', context_mode='followup')
    for work in ('understand', 'probe', 'implement', 'repair'):
        assert 'context.followup' in work_contracts(context, work)


def test_understand_view_does_not_expand_to_queued_execution_references():
    context = CustomContext(username='test')
    ids = work_contracts(
        context,
        'understand',
        ('execution.core', 'query.relational', 'result.artifacts', 'context.analysis'),
    )
    assert 'context.analysis' in ids
    assert 'execution.core' not in ids
    assert 'query.relational' not in ids
    assert 'result.artifacts' not in ids


def test_understanding_does_not_expose_code_tools_until_dependencies_loaded():
    request = ModelRequest(model=None, messages=[HumanMessage('Analyze')],
                           runtime=SimpleNamespace(context=CustomContext(username='test')),
                           tools=[SimpleNamespace(name=n) for n in (
                               'select_runtime_work', 'probe_analysis_data', 'submit_analysis_program',
                               'read_runtime_reference', 'read_runtime_datasource_context',
                           )])
    result = build_working_request(request)
    assert [t.name for t in result.tools] == ['select_runtime_work']
    assert '"work": "understand"' in result.system_message.content


def test_plan_state_exposes_material_planning_and_schema_detail_not_code_execution():
    request = ModelRequest(
        model=None,
        messages=[
            HumanMessage('Analyze equipment'),
            call('select_runtime_work', 'select', work='plan', focus='Map required facts'),
            answer('select', tool='select_runtime_work', status='success', work='plan', focus='Map required facts'),
        ],
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'plan_runtime_materials', 'probe_analysis_data',
            'submit_analysis_program', 'read_runtime_datasource_context',
        )],
    )

    result = build_working_request(request)

    assert [tool.name for tool in result.tools] == [
        'plan_runtime_materials',
        'read_runtime_datasource_context',
    ]


def test_successful_candidate_enters_review_with_only_review_actions():
    messages = [
        HumanMessage('Analyze equipment'),
        call('select_runtime_work', 'select', work='implement', focus='Analyze equipment'),
        answer('select', tool='select_runtime_work', status='success', work='implement', focus='Analyze equipment'),
        call('submit_analysis_program', 'program', code='result = save_analysis_result(...)'),
        answer(
            'program',
            tool='execute_python',
            status='success',
            execution_id=42,
            review_required=True,
            analysis_report='Equipment analysis',
        ),
    ]
    request = ModelRequest(
        model=None,
        messages=messages,
        runtime=SimpleNamespace(context=CustomContext(username='test')),
        tools=[SimpleNamespace(name=name) for name in (
            'select_runtime_work', 'plan_runtime_materials', 'probe_analysis_data',
            'submit_analysis_program', 'accept_analysis_result',
            'request_analysis_revision',
            'read_runtime_reference', 'read_runtime_datasource_context',
        )],
    )

    result = build_working_request(request)

    assert resolve_work(messages)[0] == 'review'
    assert 'execution.result_review' in work_contracts(CustomContext(username='test'), 'review')
    assert [tool.name for tool in result.tools] == [
        'accept_analysis_result',
        'request_analysis_revision',
    ]
    assert '"work": "review"' in result.system_message.content


def test_review_view_keeps_only_latest_successful_program_and_result():
    messages = [
        HumanMessage('Analyze equipment'),
        call('submit_analysis_program', 'failed', code='old_program()'),
        answer('failed', tool='execute_python', status='failed', error_type='runtime_error'),
        call('submit_analysis_program', 'candidate', code='candidate_program()'),
        answer(
            'candidate',
            tool='execute_python',
            status='success',
            execution_id=42,
            review_required=True,
            analysis_report='Candidate report',
        ),
    ]

    projected, archived = project_history(messages, work='review')

    sources = [
        tool_call.get('args', {}).get('code')
        for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
        if tool_call.get('name') == 'submit_analysis_program'
    ]
    assert sources == ['candidate_program()']
    assert any(
        isinstance(message, ToolMessage)
        and json.loads(message.content).get('analysis_report') == 'Candidate report'
        for message in projected
    )
    assert archived >= 1


def test_review_revision_moves_back_to_formal_repair():
    messages = [
        call('submit_analysis_program', 'candidate', code='candidate_program()'),
        answer('candidate', tool='execute_python', status='success', execution_id=42),
        call('request_analysis_revision', 'review', issues=[{
            'issue_type': 'calculation',
            'description': 'The comparison direction is reversed.',
            'required_correction': 'Use the documented numerator and denominator.',
        }]),
        answer('review', tool='request_analysis_revision', status='revision_required', issues=[]),
    ]

    assert resolve_work(messages)[0] == 'repair'


def test_latest_review_revision_is_active_in_repair_view():
    messages = [
        call('request_analysis_revision', 'old-review', issues=[{'description': 'old'}]),
        answer('old-review', tool='request_analysis_revision', status='revision_required'),
        call('request_analysis_revision', 'current-review', issues=[{'description': 'current'}]),
        answer('current-review', tool='request_analysis_revision', status='revision_required'),
    ]

    projected, archived = project_history(messages, work='repair')

    assert archived == 1
    assert [
        tool_call['id'] for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ] == ['current-review']
    assert [
        message.tool_call_id for message in projected if isinstance(message, ToolMessage)
    ] == ['current-review']


def test_review_revision_is_archived_after_new_candidate_is_ready():
    messages = [
        call('submit_analysis_program', 'candidate-1', code='first_candidate()'),
        answer('candidate-1', tool='execute_python', status='success', execution_id=41),
        call('request_analysis_revision', 'review-1', issues=[{'description': 'fix formula'}]),
        answer('review-1', tool='request_analysis_revision', status='revision_required'),
        call('submit_analysis_program', 'candidate-2', code='corrected_candidate()'),
        answer('candidate-2', tool='execute_python', status='success', execution_id=42),
    ]

    projected, archived = project_history(messages, work='review')

    assert archived >= 2
    calls = [
        tool_call for message in projected if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ]
    assert [(item['name'], item['id']) for item in calls] == [
        ('submit_analysis_program', 'candidate-2'),
    ]
    assert all(
        not isinstance(message, ToolMessage) or message.tool_call_id != 'review-1'
        for message in projected
    )


def test_working_request_replaces_initial_schema_envelope_with_selected_material_view():
    source = {
        'datasource_id': 12,
        'datasource_name': 'orders',
        'datasource_identifier': 'dw.orders',
        'datasource_type': 'table',
        'metadata_schema': {
            'properties': {
                'amount': {'type': 'number', 'description': 'Actual order amount.'},
                'status': {'type': 'string', 'description': 'Order lifecycle status.'},
            },
            'relationships': [{'relationship_id': 'duplicated-in-index'}],
        },
    }
    active = {
        'selected_datasource_ids': [12],
        'selected_datasource_snapshot': [source],
        'analysis_semantic_context': {
            'business_goal': 'Analyze orders',
            'business_questions': [{'question_id': 'q1', 'business_question': 'How much?'}],
            'insight_lineage': [{
                'question_id': 'q1', 'datasource_ids': [12],
                'fact_bindings': [{'fact_id': 'f1', 'datasource_id': 12, 'fields': ['amount']}],
            }],
            'relationships': [],
        },
    }
    messages = [
        SystemMessage(
            '<runtime-datasource-envelope>\nstale full schema\n</runtime-datasource-envelope>\nBase rules'
        ),
        HumanMessage('Analyze orders'),
        call('select_runtime_work', 'select', work='implement', focus='Compute amount',
             datasource_ids=[12], field_names_by_datasource={'12': ['amount']}),
        answer('select', tool='select_runtime_work', status='success', work='implement',
               focus='Compute amount', datasource_ids=[12], field_names_by_datasource={'12': ['amount']}),
    ]
    request = ModelRequest(
        model=None,
        messages=messages,
        runtime=SimpleNamespace(context=CustomContext(
            username='test', active_datasource_snapshot=active,
            execution_datasource_snapshot=copy.deepcopy(active),
        )),
        tools=[SimpleNamespace(name=name) for name in ('select_runtime_work', 'submit_analysis_program')],
    )
    result = build_working_request(request)
    content = result.system_message.content
    assert content.count('<runtime-datasource-envelope>') == 1
    assert 'stale full schema' not in content
    assert 'Actual order amount.' in content
    assert 'Order lifecycle status.' not in content
    assert content.count('duplicated-in-index') == 1


def test_actual_agent_loop_changes_tools_and_material_before_code_generation():
    from langchain.agents import create_agent
    from langchain.tools import tool
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field
    from agent.runtime_working_context import (
        RuntimeWorkingContextMiddleware, plan_runtime_materials,
        request_runtime_probe, select_runtime_work,
    )
    from agent.runtime_workspace import read_runtime_datasource_context

    class RecordingModel(BaseChatModel):
        seen: list = Field(default_factory=list)
        bound: list = Field(default_factory=list)

        @property
        def _llm_type(self):
            return 'working-context-test'

        def bind_tools(self, tools, **kwargs):
            self.bound.append([getattr(item, 'name', None) for item in tools])
            return self

        def _generate(self, messages, **kwargs):
            self.seen.append(messages)
            responses = [
                call('select_runtime_work', 'select', work='plan', focus='Analyze equipment'),
                call('plan_runtime_materials', 'plan', focus='Analyze equipment', material_units=[{
                    'question_ids': ['q1'], 'fact_ids': ['f1'], 'datasource_id': 1,
                    'field_names': ['state'], 'relationship_ids': [], 'purpose': 'Read state facts.',
                }]),
                call('read_runtime_datasource_context', 'read', datasource_ids=[1],
                     field_names_by_datasource={'1': ['state']}),
                call('request_runtime_probe', 'new_dependency', focus='Verify state values'),
                call('probe_analysis_data', 'probe', code='inspect states'),
                call('submit_analysis_program', 'execute', code='complete program'),
                AIMessage(content='finished'),
            ]
            return ChatResult(generations=[ChatGeneration(message=responses[len(self.seen) - 1])])

    @tool
    def submit_analysis_program(code: str) -> dict:
        """Submit test program."""
        return {'status': 'success'}

    @tool
    def probe_analysis_data(code: str) -> dict:
        """Return a scripted technical observation without a database."""
        return {'status': 'observation', 'diagnostics': {'probe_fact_ledger': {
            'observations': [{'query_key': 'states', 'columns': ['state']}],
        }}}

    model = RecordingModel()
    graph = create_agent(model=model, tools=[select_runtime_work, plan_runtime_materials,
                                           request_runtime_probe,
                                           submit_analysis_program, read_runtime_datasource_context,
                                           probe_analysis_data],
                         context_schema=CustomContext, middleware=[RuntimeWorkingContextMiddleware()])
    result = graph.invoke({'messages': [HumanMessage('Analyze equipment')]},
                          context=CustomContext(username='test', execution_datasource_snapshot={
                              'selected_datasource_snapshot': [{
                                  'datasource_id': 1,
                                  'metadata_schema': {'properties': {
                                      'state': {'type': 'string', 'description': 'Equipment state enum: running, stopped.'},
                                  }},
                              }],
                          }))
    assert model.bound[0] == ['select_runtime_work']
    assert model.bound[1] == ['plan_runtime_materials', 'read_runtime_datasource_context']
    assert 'submit_analysis_program' in model.bound[2]
    assert 'read_runtime_datasource_context' in model.bound[2]
    assert 'submit_analysis_program' in model.bound[3]
    assert '"work": "implement"' in model.seen[3][0].content
    read_result = next(m for m in model.seen[3]
                       if isinstance(m, ToolMessage) and m.tool_call_id == 'read')
    assert 'Equipment state enum: running, stopped.' not in read_result.content
    assert model.seen[3][0].content.count('Equipment state enum: running, stopped.') == 1
    assert 'probe_analysis_data' in model.bound[4]
    assert 'submit_analysis_program' not in model.bound[4]
    assert '"work": "probe"' in model.seen[4][0].content
    assert '"work": "implement"' in model.seen[5][0].content
    assert '"focus": "Analyze equipment"' in model.seen[5][0].content
    assert 'submit_analysis_program' in model.bound[5]
    assert 'probe_analysis_data' not in model.bound[5]
    assert 'states' in model.seen[5][0].content
    assert '"loaded_field_names":["state"]' in model.seen[5][0].content
    assert 'build_chart_result' in '\n'.join(str(m.content) for m in model.seen[2])
    assert len(result['messages']) == 14
    assert all('runtime-working-context' not in str(m.content) for m in result['messages'])


def test_query_facts_deduplicate_identical_snapshots_not_changed_scope():
    from agent.runtime_working_context import collect_query_facts
    snapshot = {'query_key': 'q1', 'query_constraints': {'status': 'open'}, 'rows': 10}
    other = {**snapshot, 'query_constraints': {'status': 'closed'}}
    messages = [answer('a', query_snapshots={'q1': snapshot}),
                answer('b', query_snapshots={'q1': snapshot}),
                answer('c', query_snapshots={'q1': other})]
    saved = copy.deepcopy(messages)
    result, facts = collect_query_facts(messages)
    assert len(facts) == 2
    assert messages == saved
    for index, message in enumerate(result):
        ref = json.loads(message.content)['query_snapshot_refs']['q1']['definition_id']
        expected = snapshot if index < 2 else other
        assert facts[ref]['query_key'] == expected['query_key']
        assert facts[ref]['query_constraints'] == expected['query_constraints']
        # Previous physical source is intentionally audit-only; Runtime needs
        # its semantic scope, not a replayable SQL/program fragment.
        assert 'rows' not in facts[ref]


def test_query_fact_work_view_bounds_large_population_filters_but_reader_stays_exact():
    from agent.runtime_working_context import collect_query_facts, read_runtime_query_context

    population = [f'BATCH-{index:04d}' for index in range(300)]
    snapshot = {
        'query_key': 'risk_batches',
        'query_constraints': {
            'target_filters': [{'field_hint': 'batch_id', 'value': population}],
        },
        'analysis_scope': {
            'target_filters': [{'field_hint': 'batch_id', 'value': population}],
        },
        'execution': {'status': 'failed', 'error_type': 'query_constraint_violation'},
    }
    messages = [answer('facts', query_snapshots={'risk_batches': snapshot})]

    _projected, facts = collect_query_facts(messages)
    definition_id = next(iter(facts))
    compact = facts[definition_id]['query_constraints']['target_filters'][0]['value']
    assert compact['_truncated'] is True
    assert compact['count'] == 300
    assert len(json.dumps(facts, ensure_ascii=False)) < 12000

    exact = read_runtime_query_context.func(
        runtime=SimpleNamespace(state={'messages': messages}),
        definition_ids=[definition_id],
    )
    exact_values = exact['queries'][definition_id]['query_constraints']['target_filters'][0]['value']
    assert exact_values == population


def test_query_index_can_recover_exact_implementation_without_result_rows():
    from agent.runtime_working_context import collect_query_facts, read_runtime_query_context
    first = {'query_key': 'q1', 'implementation': {'sql': 'SELECT sum(amount) FROM orders'},
             'execution': {'status': 'success', 'row_count': 1}, 'rows': [{'amount': 42}]}
    second = {**first, 'implementation': {'sql': 'SELECT sum(amount) FROM orders WHERE closed = true'}}
    messages = [answer('snapshots', query_snapshots={'all': first, 'closed': second})]
    original = copy.deepcopy(messages)
    projected, facts = collect_query_facts(messages)
    assert len(facts) == 2
    assert all('sql' not in item['implementation'] for item in facts.values())
    ids = list(facts)
    detail = read_runtime_query_context.func(
        runtime=SimpleNamespace(state={'messages': messages}), definition_ids=ids + ['missing'],
    )
    assert detail['status'] == 'partial'
    assert detail['unavailable_definition_ids'] == ['missing']
    assert {v['implementation']['sql'] for v in detail['queries'].values()} == {
        first['implementation']['sql'], second['implementation']['sql'],
    }
    assert all('rows' not in item for item in detail['queries'].values())
    assert messages == original
    assert 'SELECT' not in str(projected)


def test_query_reader_does_not_change_repair_state_or_load_until_facts_exist():
    tools = [SimpleNamespace(name=n) for n in ('read_runtime_query_context', 'submit_analysis_program')]
    context = SimpleNamespace(context=CustomContext(username='test'))
    empty = build_working_request(ModelRequest(model=None, messages=[HumanMessage('Analyze')],
        runtime=context, tools=tools))
    assert 'read_runtime_query_context' not in [t.name for t in empty.tools]
    messages = [call('submit_analysis_program', 'code', code='complete'),
                answer('code', status='failed', error_type='query_error', query_snapshots={
                    'q': {'query_key': 'q', 'implementation': {'sql': 'SELECT amount FROM orders'}},
                })]
    before = resolve_work(messages)
    messages.extend([call('read_runtime_query_context', 'read', definition_ids=['q']),
                     answer('read', tool='read_runtime_query_context', status='success', queries={})])
    compiled = build_working_request(ModelRequest(model=None, messages=messages, runtime=context, tools=tools))
    assert resolve_work(messages) == before
    assert 'read_runtime_query_context' in [t.name for t in compiled.tools]


def test_local_python_repair_does_not_replay_query_context():
    tools = [SimpleNamespace(name=n) for n in ('read_runtime_query_context', 'submit_analysis_program')]
    context = SimpleNamespace(context=CustomContext(username='test'))
    messages = [
        call('submit_analysis_program', 'code', code='frame.rename(columns={"a": "A"}).sort_values("a")'),
        answer('code', status='failed', execution_id=21, error_type='schema_or_column_mismatch',
               diagnostics={'violated_contract': 'missing_mapping_key'},
               query_snapshots={'q': {
                   'query_key': 'q',
                   'implementation': {'sql': 'SELECT a FROM source'},
               }}),
        call('read_runtime_query_context', 'read', definition_ids=['q']),
        answer('read', tool='read_runtime_query_context', status='success', queries={'q': {'large': 'detail'}}),
    ]

    compiled = build_working_request(ModelRequest(model=None, messages=messages, runtime=context, tools=tools))

    assert 'read_runtime_query_context' not in [tool.name for tool in compiled.tools]
    assert 'executed-query-facts' not in compiled.system_message.content
    assert all(
        not isinstance(message, ToolMessage) or message.tool_call_id != 'read'
        for message in compiled.messages
    )


def test_actual_clarification_resume_preserves_work_materials_and_query_references(monkeypatch):
    from agent import invoker
    from agent.runtime_working_context import resolve_work_material_scope, collect_query_facts, read_runtime_query_context
    monkeypatch.setattr(invoker, '_load_turn_executions_fresh', lambda _: [])
    messages = [
        HumanMessage('Compare the full product range.'),
        answer('plan', tool='plan_runtime_materials', status='success', focus='Compare all products',
               material_units=[{'datasource_id': 11, 'field_names': ['amount']}]),
        call('submit_analysis_program', 'formal', code='current full program'),
        answer('formal', status='failed', error_type='query_error', code_error_context={'line': 8},
               query_snapshots={'q': {'query_key': 'q', 'implementation': {'sql': 'SELECT amount FROM orders'}}}),
        call('select_runtime_work', 'switch', work='probe', focus='Check time format'),
        answer('switch', tool='select_runtime_work', status='success', work='probe',
               focus='Check time format', datasource_ids=[11], field_names_by_datasource={'11': ['date']}),
        call('probe_analysis_data', 'probe', code='probe dates'),
        answer('probe', status='observation', diagnostics={'probe_fact_ledger': {'observations': [
            {'query_key': 'date_format', 'query_constraints': {}, 'columns': ['date']},
        ]}}),
    ]
    saved = invoker._build_clarification_resume_context(
        preparation=None, runtime=SimpleNamespace(context_mode='fresh_analysis'), transcript=messages,
    )
    saved['user_clarifications'] = [{'clarification_id': 'period', 'answer_text': 'Use the complete month.'}]
    saved = json.loads(json.dumps(saved))
    request = SimpleNamespace(clarification_context=saved)
    resumed = invoker._build_agent_input_with_runtime_instruction(agent_request=request, runtime=SimpleNamespace())
    assert resolve_work(resumed.messages) == resolve_work(messages)
    assert resolve_work_material_scope(resumed.messages) == ([11], {'11': ['amount']})
    assert 'Use the complete month.' in resumed.messages[-1].content
    context = SimpleNamespace(context=CustomContext(username='test'))
    tools = [SimpleNamespace(name=n) for n in ('submit_analysis_program', 'read_runtime_query_context')]
    view = build_working_request(ModelRequest(model=None, messages=resumed.messages, runtime=context, tools=tools))
    failure = next(m for m in view.messages if isinstance(m, ToolMessage) and m.tool_call_id == 'formal')
    assert json.loads(failure.content)['code_error_context'] == {'line': 8}
    _, facts = collect_query_facts(resumed.messages)
    retrieved = read_runtime_query_context.func(
        runtime=SimpleNamespace(state={'messages': resumed.messages}), definition_ids=list(facts),
    )
    assert retrieved['status'] == 'success'
    repeated = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request, runtime=SimpleNamespace(), prior_messages=resumed.messages,
    )
    assert len(repeated.messages) == len(resumed.messages)
    assert resolve_work(repeated.messages) == resolve_work(messages)


def test_formal_repair_projects_only_the_latest_execution_query_facts():
    from agent.runtime_working_context import collect_query_facts
    first = {'query_key': 'q1', 'query_constraints': {'status': 'open'}, 'rows': 10}
    latest = {'query_key': 'q2', 'query_constraints': {'status': 'closed'}, 'rows': 5}
    messages = [
        call('submit_analysis_program', 'first', code='first program'),
        answer('first', query_snapshots={'q1': first}, error_type='query_error'),
        call('submit_analysis_program', 'latest', code='latest program'),
        answer('latest', query_snapshots={'q2': latest}, error_type='query_error'),
    ]

    result, facts = collect_query_facts(messages)

    assert len(facts) == 1
    only_fact = next(iter(facts.values()))
    assert only_fact['query_key'] == 'q2'
    assert json.loads(result[1].content)['query_snapshot_refs'] == {}
    assert json.loads(result[1].content)['query_snapshot_history'] == 'superseded_by_latest_formal_execution'
    latest_ref = json.loads(result[3].content)['query_snapshot_refs']['q2']['definition_id']
    assert facts[latest_ref]['query_constraints'] == {'status': 'closed'}


def test_observation_unknown_payload_and_user_clarification_are_never_pruned():
    messages = [HumanMessage('Use the complete month, including closed orders.'),
                answer('unknown', data={'formal_program_work_packet': {'business': 'user field'}}),
                call('submit_analysis_program', 'p', code='full')]
    result, _ = project_history(messages)
    assert result[:2] == messages[:2]
    assert result[2].content == ''
    assert result[2].tool_calls == messages[2].tool_calls


def test_probe_does_not_supersede_active_formal_program_diagnostic():
    messages = [
        call('submit_analysis_program', 'formal', code='current source'),
        answer('formal', status='failed', error_type='query_error',
               code_error_context={'line': 12, 'source': 'current failing query'},
               repair_target={'execution_id': 42}),
        call('probe_analysis_data', 'probe', code='inspect date values'),
        answer('probe', status='observation', diagnostics={'probe_fact_ledger': {'observations': []}}),
    ]
    result, _ = project_history(messages)
    diagnostic = json.loads(next(m.content for m in result
                                 if isinstance(m, ToolMessage) and m.tool_call_id == 'formal'))
    assert diagnostic['code_error_context'] == {'line': 12, 'source': 'current failing query'}
    assert diagnostic['repair_target'] == {'execution_id': 42}
    assert 'history_status' not in diagnostic


def test_material_and_detail_read_memories_have_explicit_producer_types():
    messages = [
        answer('plan', tool='plan_runtime_materials', status='success', material_units=[{'datasource_id': 11}]),
        answer('read', tool='read_runtime_datasource_context', status='success',
               datasources=[{'datasource_id': 11, 'metadata_schema': {'properties': {}}}],
               business_context={'insight_lineage': [{'question_id': 'q1'}]}),
    ]
    index = build_working_memory_index(messages)
    assert len(index.of_kind(WorkingMemoryKind.MATERIAL_PLAN)) == 1
    assert len(index.of_kind(WorkingMemoryKind.SCHEMA_SOURCE)) == 1
    assert len(index.of_kind(WorkingMemoryKind.PREPARATION_FACT)) == 1
    assert not index.of_kind(WorkingMemoryKind.UNKNOWN)


def test_analytical_note_attached_to_tool_call_is_not_an_untyped_memory():
    message = call('read_runtime_datasource_context', 'read', datasource_ids=[11])
    index = build_working_memory_index([message])
    assert len(index.of_kind(WorkingMemoryKind.ASSISTANT_ANALYTICAL_NOTE)) == 1
    projected, _ = project_history([message], memory=index)
    assert projected[0].content == ''
    assert projected[0].tool_calls == message.tool_calls


def test_async_middleware_uses_the_same_projection():
    import asyncio
    from agent.runtime_working_context import RuntimeWorkingContextMiddleware
    request = ModelRequest(model=None, messages=[HumanMessage('Analyze')],
                           runtime=SimpleNamespace(context=CustomContext(username='test')))
    async def handler(projected):
        return projected
    result = asyncio.run(RuntimeWorkingContextMiddleware().awrap_model_call(request, handler))
    assert result.system_message.content == build_working_request(request).system_message.content
