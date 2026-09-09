import ast
import copy

import pytest

from agent.runtime_working_context import _compact_probe_ledger, _merge_probe_ledgers
from agent.tools import (
    CURRENT_ANALYSIS_PARAMETERS_OVERRIDE,
    _validate_analysis_parameter_defaults_contract,
    get_analysis_parameters,
)


def test_distinct_facts_survive_former_quantity_limits():
    ledger = {
        'observations': [{'query_key': f'q{i}', 'status': 'success'} for i in range(70)],
        'confirmed_facts': [{'fact_id': f'f{i}'} for i in range(50)],
        'unresolved_facts': [{'fact_id': f'u{i}'} for i in range(50)],
    }
    original = copy.deepcopy(ledger)
    result = _merge_probe_ledgers([_compact_probe_ledger(ledger)])
    assert len(result['observations']) == 70
    assert len(result['confirmed_facts']) == len(result['unresolved_facts']) == 50
    assert ledger == original


def test_observation_update_replaces_failure_not_other_source():
    result = _merge_probe_ledgers([
        {'observations': [{'query_key': 'q', 'datasource_id': 1, 'status': 'failed'}]},
        {'observations': [{'query_key': 'q', 'datasource_id': 2, 'status': 'success'}]},
        {'observations': [{'query_key': 'q', 'datasource_id': 1, 'status': 'success', 'columns': ['a']}]},
    ])
    assert len(result['observations']) == 2
    assert all(item['status'] == 'success' for item in result['observations'])


def test_explicit_fact_identity_updates_status_within_same_scope():
    pending = {'fact_id': 'format', 'datasource_id': 1, 'description': 'unknown'}
    confirmed = {**pending, 'description': 'YYYY-MM'}
    other_source = {**pending, 'datasource_id': 2}
    result = _merge_probe_ledgers([
        {'unresolved_facts': [pending, other_source]},
        {'confirmed_facts': [confirmed]},
    ])
    assert result['confirmed_facts'] == [confirmed]
    assert result['unresolved_facts'] == [other_source]
    reopened = _merge_probe_ledgers([result, {'unresolved_facts': [pending]}])
    assert reopened['confirmed_facts'] == []


def test_natural_language_facts_are_not_semantically_guessed():
    result = _merge_probe_ledgers([
        {'unresolved_facts': ['Date format unknown']},
        {'confirmed_facts': ['Date format is YYYY-MM']},
    ])
    assert result['unresolved_facts'] == ['Date format unknown']


def test_parameter_schema_misuse_reports_correct_call():
    with pytest.raises(ValueError, match='expects parameter default values'):
        get_analysis_parameters({'parameters': [{'name': 'month'}], 'defaults': {'month': '2026-03'}})


def test_static_contract_rejects_parameter_schema_as_defaults():
    code = '''
analysis_parameters_schema = {"parameters": [], "defaults": {"month": "2026-03"}}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema)
'''
    retry = _validate_analysis_parameter_defaults_contract(ast.parse(code), code)
    assert retry is not None
    assert retry.diagnostics['violated_contract'] == 'analysis_parameter_defaults_argument'
    assert 'analysis_parameters_schema["defaults"]' in retry.repair_instructions[0]


def test_parameter_overrides_remain_authoritative_and_do_not_mutate_defaults():
    defaults = {'month': '2026-03'}
    token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set({'month': '2026-04'})
    try:
        assert get_analysis_parameters(defaults) == {'month': '2026-04'}
        assert defaults == {'month': '2026-03'}
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(token)
