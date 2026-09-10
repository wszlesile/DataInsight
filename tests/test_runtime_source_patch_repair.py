import ast
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.invoker import _get_latest_failed_repair_target
from agent.tools import (
    ExePythonCodeInput,
    SourceCodePatch,
    SourcePatchResolutionError,
    _apply_source_code_patches,
    _augment_missing_mapping_key_feedback,
    _build_generated_code_error_context,
    _extend_query_dependency_ranges,
    _tool_error_message,
    _resolve_execute_python_source,
    _review_source_patch_range,
    _validate_generated_code_contract,
    execute_python,
)


class RuntimeSourcePatchRepairTest(unittest.TestCase):
    def test_review_anchor_resolves_to_smallest_complete_statement(self):
        source = (
            'rate = revenue / cost\n'
            'report = f"gross margin is {rate:.2%}"\n'
            'result = save_analysis_result(analysis_report=report)\n'
        )

        target = _review_source_patch_range(source, 'gross margin is')

        self.assertEqual((target['start_line'], target['end_line']), (2, 2))
        self.assertEqual(target['source'], 'report = f"gross margin is {rate:.2%}"')

    def test_review_anchor_resolves_unique_elided_block(self):
        source = (
            'evidence = {\n'
            '    "process": process_count,\n'
            '    "equipment": equipment_count,\n'
            '    "packaging": 0,\n'
            '}\n'
            'report = "done"\n'
        )

        target = _review_source_patch_range(
            source,
            'evidence = {\n    "process": process_count,\n    ...\n    "packaging": 0,\n}',
        )

        self.assertEqual((target['start_line'], target['end_line']), (1, 5))
        self.assertIn('"equipment": equipment_count', target['source'])

    def test_review_patch_uses_latest_authorized_successful_candidate(self):
        source = 'value = 1\nreport = f"value={value}"\n'
        base = SimpleNamespace(
            id=17,
            execution_status='success',
            generated_code=source,
            used_datasource_ids_json='[3, 4]',
            result_payload_json=json.dumps({'review_revision_target': {
                'mode': 'review_line_patch',
                'base_execution_id': 17,
                'base_source_sha256': __import__('hashlib').sha256(source.strip().encode()).hexdigest(),
                'allowed_ranges': [{'start_line': 2, 'end_line': 2}],
            }}),
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base
        session.query.return_value.filter.return_value.order_by.return_value.first.return_value = base
        with patch('config.database.SessionLocal', return_value=session):
            repaired, ids, parent, kind = _resolve_execute_python_source(
                conversation_id=1,
                turn_id=2,
                code='',
                used_datasource_ids=[],
                base_execution_id=17,
                patches=[SourceCodePatch(
                    start_line=2,
                    end_line=2,
                    replacement='report = f"corrected={value}"',
                )],
            )

        self.assertEqual(repaired, 'value = 1\nreport = f"corrected={value}"\n')
        self.assertEqual(ids, [3, 4])
        self.assertEqual(parent, 17)
        self.assertEqual(kind, 'llm_review_patch')

    def test_review_patch_rejects_unapproved_statement(self):
        source = 'value = 1\nreport = f"value={value}"\n'
        base = SimpleNamespace(
            id=17,
            execution_status='success',
            generated_code=source,
            used_datasource_ids_json='[3]',
            result_payload_json=json.dumps({'review_revision_target': {
                'mode': 'review_line_patch',
                'base_execution_id': 17,
                'base_source_sha256': __import__('hashlib').sha256(source.strip().encode()).hexdigest(),
                'allowed_ranges': [{'start_line': 2, 'end_line': 2}],
            }}),
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base
        session.query.return_value.filter.return_value.order_by.return_value.first.return_value = base
        with patch('config.database.SessionLocal', return_value=session):
            with self.assertRaisesRegex(ValueError, 'allowed_ranges'):
                _resolve_execute_python_source(
                    conversation_id=1,
                    turn_id=2,
                    code='',
                    used_datasource_ids=[],
                    base_execution_id=17,
                    patches=[SourceCodePatch(
                        start_line=1,
                        end_line=1,
                        replacement='value = 2',
                    )],
                )

    def test_failed_patch_returns_original_revision_and_all_input_errors(self):
        source = 'frame = load(\n    sql="select amount from records",\n)\nprint(frame["status"])\n'
        base = SimpleNamespace(
            execution_status='failed',
            generated_code=source,
            used_datasource_ids_json='[3, 4]',
            result_payload_json=json.dumps({'repair_target': {
                'base_execution_id': 17, 'mode': 'line_patch',
            }}),
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base
        with patch('config.database.SessionLocal', return_value=session):
            with self.assertRaises(SourcePatchResolutionError) as caught:
                _resolve_execute_python_source(
                    conversation_id=1, turn_id=2, code='', used_datasource_ids=[3],
                    base_execution_id=17,
                    patches=[SourceCodePatch(start_line=1, end_line=1, replacement='')],
                )
        error = caught.exception
        self.assertIn('used_datasource_ids', str(error))
        self.assertIn('完整替换一个 Python 语句', str(error))
        self.assertEqual(error.source, source)
        self.assertEqual(error.diagnostics['inherited_used_datasource_ids'], [3, 4])
        self.assertFalse(error.diagnostics['new_execution_created'])
        self.assertNotIn('candidate_syntax_error', error.diagnostics)

        runtime = SimpleNamespace(context=SimpleNamespace(conversation_id=1, turn_id=2), tool_call_id='patch')
        with (
            patch('agent.tools._resolve_execute_python_source', side_effect=error),
            patch('agent.tools._raise_if_runtime_task_cancelled'),
            patch('agent.tools._get_stream_emitter'),
            patch('agent.tools._create_execution_record') as create,
        ):
            message = execute_python.func(runtime=runtime, base_execution_id=17)
        payload = json.loads(message.content)
        self.assertNotIn('repair_target', payload)
        self.assertEqual(payload['current_failed_code'], source)
        self.assertEqual(payload['code_error_context']['location_domain'], 'python_source')
        create.assert_not_called()
        session.close.assert_called_once()

    def test_authorized_syntax_patch_preserves_the_rest_of_the_program(self):
        source = (
            'items = [{"name": "A"}]\n'
            'table = build_markdown_table(items, columns=[\n'
            '    {"key": "name", "label": "Name")}])\n'
            'result = table\n'
        )

        repaired = _apply_source_code_patches(
            source,
            [SourceCodePatch(
                start_line=3,
                end_line=3,
                replacement='    {"key": "name", "label": "Name"}])',
            )],
            authorized_ranges={(3, 3)},
        )

        self.assertEqual(repaired.splitlines()[0], source.splitlines()[0])
        self.assertEqual(repaired.splitlines()[1], source.splitlines()[1])
        self.assertEqual(repaired.splitlines()[3], source.splitlines()[3])
        ast.parse(repaired)

    def test_authorized_syntax_patch_must_cover_compiler_error_line(self):
        source = 'prefix = "ok"\ntext = f"{value[\\"name\\"]}"\nresult = text\n'

        with self.assertRaisesRegex(ValueError, '必须覆盖'):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=1, end_line=1, replacement='prefix = "fixed"')],
                authorized_ranges={(1, 1)},
            )

    def test_resolver_applies_authorized_syntax_patch_to_stored_revision(self):
        source = (
            'items = [{"name": "A"}]\n'
            'table = build_markdown_table(items, columns=[\n'
            '    {"key": "name", "label": "Name")}])\n'
            'result = table\n'
        )
        base = SimpleNamespace(
            execution_status='failed',
            generated_code=source,
            used_datasource_ids_json='[3]',
            result_payload_json=json.dumps({'repair_target': {
                'base_execution_id': 17,
                'mode': 'line_patch',
                'allowed_ranges': [{'start_line': 3, 'end_line': 3}],
            }}),
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base

        with patch('config.database.SessionLocal', return_value=session):
            repaired, ids, parent, kind = _resolve_execute_python_source(
                conversation_id=1,
                turn_id=2,
                code='',
                used_datasource_ids=[],
                base_execution_id=17,
                patches=[SourceCodePatch(
                    start_line=3,
                    end_line=3,
                    replacement='    {"key": "name", "label": "Name"}])',
                )],
            )

        self.assertEqual(ids, [3])
        self.assertEqual(parent, 17)
        self.assertEqual(kind, 'llm_patch_repair')
        ast.parse(repaired)
        self.assertEqual(repaired.splitlines()[0], source.splitlines()[0])
        self.assertEqual(repaired.splitlines()[3], source.splitlines()[3])

    def test_patch_scope_is_a_set_and_still_inherited_from_base(self):
        base = SimpleNamespace(
            execution_status='failed', generated_code='value = 1\n', used_datasource_ids_json='[3, 4]',
            result_payload_json=json.dumps({'repair_target': {
                'base_execution_id': 17, 'mode': 'line_patch',
            }}),
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base
        with patch('config.database.SessionLocal', return_value=session):
            source, ids, parent, kind = _resolve_execute_python_source(
                conversation_id=1, turn_id=2, code='', used_datasource_ids=[4, 3],
                base_execution_id=17,
                patches=[SourceCodePatch(start_line=1, end_line=1, replacement='value = 2')],
            )
        self.assertEqual(source, 'value = 2\n')
        self.assertEqual(ids, [3, 4])
        self.assertEqual(parent, 17)
        self.assertEqual(kind, 'llm_patch_repair')

    def test_patch_rejects_a_failure_without_backend_patch_authorization(self):
        base = SimpleNamespace(
            execution_status='failed', generated_code='value = 1\n',
            used_datasource_ids_json='[3]', result_payload_json='{}',
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base
        with patch('config.database.SessionLocal', return_value=session):
            with self.assertRaisesRegex(ValueError, '完整程序修复'):
                _resolve_execute_python_source(
                    conversation_id=1, turn_id=2, code='', used_datasource_ids=[3],
                    base_execution_id=17,
                    patches=[SourceCodePatch(start_line=1, end_line=1, replacement='value = 2')],
                )

    def test_key_error_targets_mapping_construction_not_unrelated_namesakes(self):
        source = (
            'other = load(sql="select status from other")\n'
            'detail = load(\n'
            '    sql="select amount from records",\n'
            ')\n'
            'detail = detail.copy()\n'
            'def unrelated():\n'
            '    detail = {"status": "ok"}\n'
            '    return detail["status"]\n'
            'result = detail["status"]\n'
            'print(other["status"])\n'
        )
        generic_context = _build_generated_code_error_context(source, KeyError('status'), '')
        generic_context['line_no'] = 9
        diagnostics, _, context = _augment_missing_mapping_key_feedback(
            code=source, error_type='schema_or_column_mismatch', error_message="'status'\n",
            diagnostics={}, repair_instructions=[], code_error_context=generic_context,
        )
        self.assertEqual(diagnostics['usage_lines'], [9])
        self.assertEqual(
            [(r['start_line'], r['end_line']) for r in context['suggested_patch_ranges']],
            [(9, 9), (2, 4), (5, 5)],
        )
        payload = json.loads(_tool_error_message(
            tool_call_id='key', execution_id=17, error_type='schema_or_column_mismatch',
            error_message="'status'", repair_instructions=[], current_failed_code=source,
            code_error_context=context,
        ).content)
        self.assertEqual(payload['repair_target']['base_execution_id'], 17)
        self.assertEqual(
            [(item['start_line'], item['end_line']) for item in payload['repair_target']['allowed_ranges']],
            [(9, 9), (2, 4), (5, 5)],
        )
        self.assertEqual(payload['current_failed_code'], '')

    def test_key_error_identifies_post_rename_operation_as_single_repair_target(self):
        source = (
            'table = build_table_result(\n'
            '    data=frame[["metric"]].rename(\n'
            '        columns={"metric": "指标"}\n'
            '    ).sort_values("metric")\n'
            ')\n'
        )
        context = _build_generated_code_error_context(source, KeyError('metric'), '')
        context['line_no'] = 4

        diagnostics, instructions, context = _augment_missing_mapping_key_feedback(
            code=source,
            error_type='schema_or_column_mismatch',
            error_message="'metric'\n",
            diagnostics={},
            repair_instructions=[],
            code_error_context=context,
        )

        self.assertEqual(diagnostics['violated_contract'], 'dataframe_post_transform_key_mismatch')
        self.assertEqual(diagnostics['renamed_to'], '指标')
        self.assertEqual(
            [(item['start_line'], item['end_line']) for item in context['suggested_patch_ranges']],
            [(1, 5)],
        )
        self.assertIn('before sort_values', instructions[0])

    def test_patch_changes_only_the_addressed_lines(self):
        source = "a = 1\nb = (\n    2\n)\nc = a + b\nprint(c)\n"

        repaired = _apply_source_code_patches(
            source,
            [SourceCodePatch(start_line=2, end_line=4, replacement="b = 3")],
        )

        self.assertEqual(repaired, "a = 1\nb = 3\nc = a + b\nprint(c)\n")

    def test_patch_rejects_overlapping_ranges(self):
        with self.assertRaisesRegex(ValueError, "不能包含重叠"):
            _apply_source_code_patches(
                "if True:\n    value = 1\nprint(value)\n",
                [
                    SourceCodePatch(start_line=1, end_line=2, replacement="value = 2"),
                    SourceCodePatch(start_line=2, end_line=2, replacement="value = 3"),
                ],
            )

    def test_tool_schema_supports_patch_only_repair(self):
        payload = ExePythonCodeInput(
            base_execution_id=676,
            patches=[{"start_line": 10, "end_line": 12, "replacement": "chart = fixed"}],
        )

        self.assertEqual(payload.code, "")
        self.assertEqual(payload.title, "")
        self.assertEqual(payload.description, "")
        self.assertEqual(payload.base_execution_id, 676)
        self.assertEqual(payload.patches[0].start_line, 10)

    def test_error_feedback_exposes_immutable_repair_target(self):
        message = _tool_error_message(
            tool_call_id="call-1",
            execution_id=676,
            error_type="chart_contract_error",
            error_message="duplicate plotting keys",
            repair_instructions=["aggregate duplicate keys"],
            current_failed_code="a = 1\nchart = broken\n",
            code_error_context={
                "line_no": 2,
                "suggested_patch_ranges": [{
                    "start_line": 2,
                    "end_line": 2,
                    "kind": "Assign",
                    "source": "chart = broken",
                    "role": "failing_statement",
                }],
            },
        )
        payload = json.loads(message.content)

        self.assertEqual(payload['repair_target']['base_execution_id'], 676)
        self.assertEqual(payload['repair_target']['allowed_ranges'][0]['start_line'], 2)
        self.assertEqual(payload['current_failed_code'], '')

    def test_key_error_feedback_locates_all_static_column_references(self):
        source = (
            'summary = frame[["candidate_type", "count"]]\n'
            'chart = build_chart_result(summary, category_field="candidate_type")\n'
            'text = f"{summary.iloc[0][\'candidate_type\']}"\n'
        )
        context = _build_generated_code_error_context(
            source,
            KeyError("candidate_type"),
            "traceback",
        )

        self.assertEqual(
            {item["line_no"] for item in context["symbol_references"]},
            {1, 2, 3},
        )
        self.assertEqual(
            {(item["start_line"], item["end_line"]) for item in context["suggested_patch_ranges"]},
            {(1, 1), (2, 2), (3, 3)},
        )

        message = _tool_error_message(
            tool_call_id="call-columns",
            execution_id=679,
            error_type="schema_or_column_mismatch",
            error_message="candidate_type",
            repair_instructions=["check columns"],
            current_failed_code=source,
            code_error_context=context,
        )
        payload = json.loads(message.content)
        self.assertEqual(payload['repair_target']['base_execution_id'], 679)
        self.assertEqual(
            {(item['start_line'], item['end_line']) for item in payload['repair_target']['allowed_ranges']},
            {(1, 1), (2, 2), (3, 3)},
        )
        self.assertIn("一次核对并修复全部相关定义", payload["repair_instructions"][0])

    def test_name_error_feedback_locates_all_variable_references(self):
        source = (
            'title = f"{week_end_exclusive}"\n'
            'report = f"period ends {week_end_exclusive}"\n'
        )
        error = NameError("name 'week_end_exclusive' is not defined")
        error.name = "week_end_exclusive"

        context = _build_generated_code_error_context(source, error, "traceback")

        self.assertEqual(
            {item["line_no"] for item in context["symbol_references"]},
            {1, 2},
        )
        self.assertEqual(len(context["suggested_patch_ranges"]), 2)

    def test_patch_rejects_expression_lines_within_a_statement(self):
        source = "query = (\n    'select * ' \n    'from demo'\n)\nresult = run(query)\n"

        with self.assertRaisesRegex(ValueError, '完整替换一个 Python 语句'):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=2, end_line=3, replacement="    'select count(*) from demo'")],
            )

    def test_patch_rejects_multiple_adjacent_statements_as_one_edit(self):
        source = 'first = 1\nsecond = 2\nresult = first + second\n'
        with self.assertRaisesRegex(ValueError, '完整替换一个 Python 语句'):
            _apply_source_code_patches(source, [
                SourceCodePatch(start_line=1, end_line=2, replacement='first = 3\nsecond = 4'),
            ])

    def test_patch_rejects_argument_edits_inside_a_statement(self):
        source = 'if True:\n    value = build(\n        field="wrong",\n    )\nresult = value\n'
        with self.assertRaisesRegex(ValueError, '完整替换一个 Python 语句'):
            _apply_source_code_patches(source, [
                SourceCodePatch(start_line=3, end_line=3, replacement='        field="correct",'),
            ])

    def test_patch_rejects_syntax_error_before_execution_is_created(self):
        source = "query = 'select * from demo'\nresult = run(query)\n"

        with self.assertRaisesRegex(ValueError, "合成后的完整代码存在语法错误"):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=1, end_line=1, replacement="query = '''select")],
            )

    def test_patch_rejects_invalid_base_source(self):
        source = (
            'items = [{"name": "A"}]\n'
            'text = f"values: {\',\'.join([f\'{row[\\"name\\"]}\' for row in items])}"\n'
            'result = text\n'
        )

        with self.assertRaisesRegex(ValueError, '无法建立可靠'):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=2, end_line=2, replacement='text = "values"')],
            )

    def test_patch_rejects_syntax_error_even_when_lines_are_known(self):
        source = (
            'items = [{"name": "A"}]\n'
            "first = f\"values: {','.join([f'{row[\\\"name\\\"]}' for row in items])}\"\n"
            "second = f\"values: {','.join([f'{row[\\\"name\\\"]}' for row in items])}\"\n"
            'result = first + second\n'
        )

        with self.assertRaisesRegex(ValueError, '无法建立可靠'):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=2, end_line=2, replacement='first = "values"')],
            )

    def test_invalid_source_patch_must_cover_the_compiler_error_line(self):
        source = 'prefix = "ok"\ntext = f"{value[\\"name\\"]}"\nresult = text\n'

        with self.assertRaisesRegex(ValueError, "无法建立可靠"):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=1, end_line=1, replacement='prefix = "fixed"')],
            )

    def test_syntax_contract_feedback_offers_a_bounded_patch_target(self):
        code = 'execution_intent = "analysis"\ntext = f"{value[\\"name\\"]}"\n'

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        ranges = retry.code_error_context["suggested_patch_ranges"]
        self.assertEqual(
            [(item["start_line"], item["end_line"]) for item in ranges],
            [(2, 2), (1, 2)],
        )
        message = _tool_error_message(
            tool_call_id="call-syntax",
            execution_id=678,
            error_type=retry.retry_type,
            error_message=retry.message,
            repair_instructions=retry.repair_instructions,
            diagnostics=retry.diagnostics,
            code_error_context=retry.code_error_context,
            current_failed_code=code,
        )
        payload = json.loads(message.content)
        self.assertEqual(payload['repair_target']['base_execution_id'], 678)
        self.assertEqual(
            [(item['start_line'], item['end_line']) for item in payload['repair_target']['allowed_ranges']],
            [(2, 2)],
        )
        self.assertTrue(payload['minimal_patch_required'])

    def test_structural_contract_error_does_not_offer_line_patch(self):
        message = _tool_error_message(
            tool_call_id="call-2",
            execution_id=677,
            error_type="missing_query_constraints",
            error_message="missing query constraints",
            repair_instructions=["add the complete contract"],
            current_failed_code="result = run()\n",
            code_error_context={
                "line_no": 1,
                "suggested_patch_ranges": [{
                    "start_line": 1,
                    "end_line": 1,
                    "kind": "Assign",
                    "source": "result = run()",
                    "role": "failing_statement",
                }],
            },
        )
        payload = json.loads(message.content)

        self.assertFalse(payload["minimal_patch_required"])
        self.assertNotIn("repair_target", payload)

    def test_query_limit_context_includes_complete_query_assignment(self):
        source = (
            "query = (\n"
            "    'select batch_id, value ' \n"
            "    'from measurements'\n"
            ")\n"
            "frame = load_data_with_sql(1, query)\n"
        )
        ranges = _extend_query_dependency_ranges(source, 5, [])

        query_range = next(
            item for item in ranges
            if item["role"] == "query_definition"
        )
        self.assertEqual((query_range["start_line"], query_range["end_line"]), (1, 4))
        self.assertIn("from measurements", query_range["source"])

    def test_cross_round_target_uses_execution_reference_not_code_preview(self):
        execution = SimpleNamespace(
            id=676,
            execution_status="failed",
            generated_code="a = 1\nchart = broken\n",
            result_payload_json=json.dumps({
                "repair_target": {
                    "base_execution_id": 676,
                    "source_sha256": "snapshot-hash",
                    "line_count": 2,
                    "mode": "line_patch",
                    "allowed_ranges": [{"start_line": 2, "end_line": 2}],
                },
                "code_error_context": {
                    "line_no": 2,
                    "context_lines": [{"line_no": 2, "code": "chart = broken"}],
                }
            }),
        )

        target = _get_latest_failed_repair_target([execution])

        self.assertEqual(target["base_execution_id"], 676)
        self.assertEqual(target["line_count"], 2)
        self.assertNotIn("generated_code", target)
        self.assertEqual(target["allowed_ranges"][0]["start_line"], 2)

    def test_cross_round_target_is_absent_when_tool_did_not_offer_patch(self):
        execution = SimpleNamespace(
            id=677,
            execution_status="failed",
            generated_code="analysis_report = f'''{broken}'''\n",
            result_payload_json=json.dumps({
                "retry_type": "contract_error",
                "diagnostics": {"violated_contract": "invalid_python_syntax"},
                "repair_instructions": ["submit complete code"],
            }),
        )

        self.assertEqual(_get_latest_failed_repair_target([execution]), {})


if __name__ == "__main__":
    unittest.main()
