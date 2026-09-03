"""Counterexamples for new preflight checks against executable Python behavior."""

import ast
import json

import pandas as pd
import pytest

from agent import tools


@pytest.mark.parametrize('body', [
    'detail = source[["value"]].copy()\ndetail = source.copy()\nanswer = detail["extra"].sum()',
    'detail = source[["value"]].copy()\ndetail = detail.assign(extra=source["extra"])\nanswer = detail["extra"].sum()',
    'detail = source[["value"]].copy()\ndetail.loc[:, "extra"] = source["extra"]\nanswer = detail["extra"].sum()',
    'def enrich(frame):\n    frame["extra"] = 2\ndetail = source[["value"]].copy()\nenrich(detail)\nanswer = detail["extra"].sum()',
    'detail = source[["value"]].copy()\ndef summarize(detail):\n    return detail["extra"].sum()\nanswer = summarize(source)',
])
def test_executable_dataframe_program_is_not_rejected_by_projection_inference(body):
    namespace = {'source': pd.DataFrame({'value': [1], 'extra': [2]})}
    exec(body, namespace)
    assert namespace['answer'] == 2
    retry = tools._validate_dataframe_subset_column_contract(ast.parse(body), body)
    assert retry is None or retry.diagnostics.get('proof_state') == 'unknown'
    program = '''execution_intent = "analysis"
query_constraints = {"time_range": None, "target_filters": [], "aggregation": None}
source = query_data_with_sql(query_key="source", query_constraints=query_constraints, sql="SELECT value, extra FROM data")
''' + body + '''
result = save_analysis_result(analysis_report=f"Result: {answer}", charts=[], tables=[build_table_result("Summary", data=[{"total": answer}])])
'''
    assert tools._validate_generated_code_contract(program) is None


def test_consumption_by_earlier_defined_closure_is_not_proven_unused():
    body = 'def summarize():\n    return detail["value"].sum()\ndetail = query_data_with_sql()\nanswer = summarize()'
    namespace = {'query_data_with_sql': lambda: pd.DataFrame({'value': [3]})}
    exec(body, namespace)
    assert namespace['answer'] == 3
    retry = tools._validate_analysis_query_result_consumption_contract(ast.parse(body), body)
    assert retry is None or retry.diagnostics.get('proof_state') == 'unknown'


def test_sql_projection_cannot_prove_columns_missing_after_rebinding():
    body = 'sql = "SELECT value FROM data"\ndetail = query_data_with_sql(sql=sql)\ndetail = detail.assign(extra=2)\nanswer = detail["extra"].sum()'
    namespace = {'query_data_with_sql': lambda **_: pd.DataFrame({'value': [1]})}
    exec(body, namespace)
    assert namespace['answer'] == 2
    retry = tools._validate_query_dataframe_projection_contract(body)
    assert retry is None or retry.diagnostics.get('proof_state') == 'unknown'


def test_report_feedback_locates_source_beyond_code_preview_without_rewriting_analysis():
    source = 'prefix = "' + 'x' * 6000 + '"\n' + '\n' * 18 + 'report = "## Overview\\nValue: 3\\n\\n### Detail\\n\\n### Notes\\nAvailable."\n'
    namespace = {}
    exec(source, namespace)
    retry = tools._validate_structured_result_contract(
        tools.StructuredResult(analysis_report=namespace['report'], charts=[], tables=[]),
        generated_code=source,
    )
    assert retry.diagnostics['violated_contract'] == 'empty_markdown_leaf_sections'
    assert retry.diagnostics['location_domain'] == 'analysis_report'
    assert retry.code_error_context['location_domain'] == 'python_source'
    message = tools._tool_error_message(
        tool_call_id='audit', execution_id=9, error_type=retry.retry_type,
        error_message=retry.message, repair_instructions=retry.repair_instructions,
        diagnostics=retry.diagnostics, code_error_context=retry.code_error_context,
        current_failed_code=source,
    )
    payload = json.loads(message.content)
    target = payload['repair_target']
    assert target['location_domain'] == 'python_source'
    assert target['base_execution_id'] == 9
    assert target['allowed_ranges'][0]['start_line'] == 20
    assert '## Detail' in target['allowed_ranges'][0]['source']


def test_current_repair_instructions_are_not_truncated_like_old_history():
    instructions = [f'Fix {index}' for index in range(9)] + ['Preserve all requested business paths.']
    message = tools._tool_error_message(
        tool_call_id='audit', error_type='chart_contract_error', error_message='duplicate keys',
        repair_instructions=instructions,
    )
    assert json.loads(message.content)['repair_instructions'] == instructions


def test_known_projection_defect_still_blocks_and_runtime_checks_unknown_columns():
    source = 'detail = source[["value"]].copy()\nanswer = detail["missing"].sum()'
    retry = tools._validate_dataframe_subset_column_contract(ast.parse(source), source)
    assert retry is not None and retry.diagnostics['proof_state'] == 'proven'
    with pytest.raises(KeyError):
        exec(source, {'source': pd.DataFrame({'value': [1]})})


def test_report_source_locator_does_not_guess_dynamic_or_ambiguous_heading_source():
    for source in (
        'report = prefix + heading + suffix',
        'first = "### Detail\\n"\nsecond = "### Detail\\n"',
    ):
        assert tools._locate_report_section_source(source, [{'level': 3, 'heading': 'Detail'}]) == {}


def test_static_contexts_are_flattened_to_verified_source_ranges():
    source = 'value = 1\nreport = str(value)\n'
    context = tools._static_node_code_context(source, ast.parse(source).body[1])
    message = tools._tool_error_message(
        tool_call_id='audit', execution_id=8, error_type='contract_error',
        error_message='local contract', repair_instructions=[], current_failed_code=source,
        code_error_context={'related_contexts': [context], 'suggested_patch_ranges': [context]},
    )
    ranges = json.loads(message.content)['repair_target']['allowed_ranges']
    assert len(ranges) == 1
    assert ranges[0]['source'] == 'report = str(value)' and ranges[0]['start_line'] == 2


def test_report_or_other_revision_ranges_are_not_used_as_python_patch_locations():
    for context in (
        {'location_domain': 'analysis_report', 'suggested_patch_ranges': [
            {'start_line': 1, 'end_line': 1, 'source': 'value = 1'}]},
        {'suggested_patch_ranges': [{'start_line': 1, 'end_line': 1, 'source': 'value = 2'}]},
    ):
        assert tools._verified_source_patch_ranges('value = 1\n', context) == []
    source = 'value = (\r\n    1\r\n)'
    context = {'suggested_patch_ranges': [{'start_line': 1, 'end_line': 3, 'source': source}]}
    assert len(tools._verified_source_patch_ranges(source, context)) == 1


def test_query_constraints_are_evaluated_at_the_call_not_from_future_assignments():
    source = '''scope = {"time_range": None, "target_filters": [{"semantic_role": "target", "value": "A", "match_mode": "exact"}], "aggregation": None}
first = query_data_with_sql(query_key="first", query_constraints=scope, sql="SELECT value FROM data WHERE kind = 'A'")
scope = {"time_range": None, "target_filters": [{"semantic_role": "target", "value": "B", "match_mode": "exact"}], "aggregation": None}
second = query_data_with_sql(query_key="second", query_constraints=scope, sql="SELECT value FROM data WHERE kind = 'B'")
scope = {"target_filters": "not-a-filter-list"}
'''
    assert tools._validate_static_composed_query_constraints_contract(ast.parse(source), source) is None


def test_unknown_reassignment_invalidates_previous_literal_scope():
    source = '''scope = {"target_filters": "not-a-filter-list"}
scope = make_actual_scope()
data = query_data_with_sql(query_key="data", query_constraints=scope, sql="SELECT value FROM data")
'''
    assert tools._validate_static_composed_query_constraints_contract(ast.parse(source), source) is None


def test_data_derived_parameter_does_not_prove_entity_scope_inheritance():
    source = '''baseline = query_data_with_sql(query_key="baseline", query_constraints={"time_range": None, "target_filters": [], "aggregation": None}, sql="SELECT AVG(value) AS mean_value FROM reference")
threshold = baseline["mean_value"].iloc[0]
exceeding = query_data_with_sql(query_key="exceeding", query_constraints={"time_range": None, "target_filters": [], "aggregation": None}, sql="SELECT value FROM data WHERE value > %s", params=[threshold])
'''
    retry = tools._validate_static_composed_query_constraints_contract(ast.parse(source), source)
    assert retry is None or retry.diagnostics.get('proof_state') == 'unknown'
