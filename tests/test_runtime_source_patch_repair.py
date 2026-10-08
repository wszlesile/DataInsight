import ast
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.invoker import _get_latest_failed_repair_target
from agent.tools import (
    AnalysisRevisionIssue,
    ExePythonCodeInput,
    FullProgramRequiredError,
    SourceCodePatch,
    SourcePatchResolutionError,
    _apply_source_code_patches,
    _augment_missing_mapping_key_feedback,
    _build_generated_code_error_context,
    _build_analysis_logic_signature,
    _extend_query_dependency_ranges,
    _named_assignment_patch_range,
    _tool_error_message,
    _resolve_execute_python_source,
    _review_source_patch_range,
    _validate_generated_code_contract,
    execute_python,
    request_analysis_revision,
)


class RuntimeSourcePatchRepairTest(unittest.TestCase):
    def test_valid_source_patch_respects_explicit_authorized_ranges(self):
        source = 'value = 1\nreport = str(value)\n'
        patch_item = SourceCodePatch(start_line=1, end_line=1, replacement='value = 2')
        for allowed in ({(2, 2)}, set()):
            with self.subTest(allowed=allowed):
                with self.assertRaisesRegex(ValueError, 'repair_target.allowed_ranges'):
                    _apply_source_code_patches(source, [patch_item], authorized_ranges=allowed)
        self.assertEqual(
            _apply_source_code_patches(source, [patch_item], authorized_ranges={(1, 1)}),
            'value = 2\nreport = str(value)\n',
        )
        self.assertEqual(
            _apply_source_code_patches(source, [patch_item]),
            'value = 2\nreport = str(value)\n',
        )

    def test_assignment_repair_range_identifies_output_without_inference(self):
        from agent.tools import _source_statement_patch_ranges

        source = 'report = f"""Summary\n{missing_name}\n"""\nresult = save(report)\n'
        ranges = _source_statement_patch_ranges(source, 2)
        self.assertEqual(ranges[0]['defined_names'], ['report'])
        self.assertEqual(ranges[0]['source'], '\n'.join(source.splitlines()[:3]))
        self.assertEqual((ranges[0]['start_line'], ranges[0]['end_line']), (1, 3))
        self.assertEqual(
            _source_statement_patch_ranges('left, right = calculate()\n', 1)[0]['defined_names'],
            ['left', 'right'],
        )
        self.assertEqual(
            _source_statement_patch_ranges('frame["column"] = calculate()\n', 1)[0]['defined_names'],
            [],
        )

    def test_located_report_formatting_failure_authorizes_statement_patch(self):
        from agent.tools import _find_unrendered_report_placeholders, _locate_unrendered_placeholder_source

        source = 'total = 10\nreport = """Count: {total}\nComplete report\n"""\nresult = report\n'
        placeholders = _find_unrendered_report_placeholders('Count: {total}', generated_code=source)
        context = _locate_unrendered_placeholder_source(source, placeholders)
        for error_type in ('contract_error', 'result_contract_error'):
            payload = json.loads(_tool_error_message(
                tool_call_id='call', execution_id=17, error_type=error_type,
                error_message='Unrendered expression', repair_instructions=[],
                diagnostics={'violated_contract': 'unrendered_report_placeholder',
                             'placeholders': placeholders},
                code_error_context=context, current_failed_code=source,
            ).content)
            target = payload['repair_target']
            self.assertEqual(target['source_sha256'], _build_analysis_logic_signature(source))
            self.assertEqual([(r['start_line'], r['end_line']) for r in target['allowed_ranges']], [(2, 4)])
            repaired = _apply_source_code_patches(source, [SourceCodePatch(
                start_line=2, end_line=4,
                replacement='report = f"""Count: {total}\nComplete report\n"""',
            )])
            namespace = {}
            exec(compile(repaired, '<generated_analysis_code>', 'exec'), namespace)
            self.assertEqual(namespace['report'], 'Count: 10\nComplete report\n')
        unlocated = json.loads(_tool_error_message(
            tool_call_id='call', execution_id=17, error_type='result_contract_error',
            error_message='Unrendered expression', repair_instructions=[],
            diagnostics={'violated_contract': 'unrendered_report_placeholder',
                         'placeholders': placeholders}, current_failed_code=source,
        ).content)
        self.assertNotIn('repair_target', unlocated)

    def test_type_error_exposes_consecutive_same_callable_repair_locations(self):
        source = (
            'lines = []\n'
            'lines.append("first", "continuation")\n'
            'lines.append("second", "continuation")\n'
            'lines.append("third", "continuation", "ending")\n'
            'lines.append("valid")\n'
            'unrelated = 1\n'
            'lines.append("later", "outside")\n'
        )
        try:
            exec(compile(source, '<generated_analysis_code>', 'exec'), {})
        except TypeError as error:
            context = _build_generated_code_error_context(source, error, '')
        self.assertEqual(context['line_no'], 2)
        self.assertEqual(
            [item['line_no'] for item in context['related_contexts']], [3, 4, 5],
        )
        self.assertTrue(all(item['confirmed_error'] is False for item in context['related_contexts']))
        payload = json.loads(_tool_error_message(
            tool_call_id='call', execution_id=17, error_type='library_api_signature_mismatch',
            error_message='list.append signature mismatch', repair_instructions=[],
            code_error_context=context, current_failed_code=source,
        ).content)
        self.assertEqual(
            [item['start_line'] for item in payload['repair_target']['allowed_ranges']],
            [2, 3, 4, 5],
        )
        repaired = _apply_source_code_patches(source, [
            SourceCodePatch(start_line=line, end_line=line, replacement=f'lines.append("fixed {line}")')
            for line in (2, 3, 4)
        ])
        self.assertIn('lines.append("valid")', repaired)
        self.assertIn('lines.append("later", "outside")', repaired)
        self.assertIn('不是已确认错误', payload['repair_instructions'][0])

    def test_related_calls_do_not_cross_callables_or_rebindings(self):
        from agent.tools import _consecutive_call_repair_contexts

        for separator in ('other.append("ok")', 'lines = []', 'if True:\n    lines.append("nested")'):
            source = 'lines.append("a", "b")\n' + separator + '\nlines.append("c", "d")\n'
            self.assertEqual(_consecutive_call_repair_contexts(source, 1), [])
        self.assertEqual(_consecutive_call_repair_contexts('value = lines.append("a", "b")', 1), [])

    def test_complete_program_scope_is_required_in_the_patch_capable_tool_schema(self):
        from pydantic import ValidationError
        from agent.tools import SubmitAnalysisProgramInput

        for source_key in ('code', 'program', 'analysis_code', 'analysis_program'):
            with self.subTest(source_key=source_key):
                with self.assertRaises(ValidationError):
                    SubmitAnalysisProgramInput.model_validate({
                        source_key: 'result = build_report()', 'base_execution_id': 17,
                    })
                request = SubmitAnalysisProgramInput.model_validate({
                    source_key: 'result = build_report()', 'used_datasource_ids': '[7]',
                    'base_execution_id': 17,
                })
                self.assertEqual(request.code, 'result = build_report()')
                self.assertEqual(request.used_datasource_ids, [7])

    def test_code_free_statement_patch_keeps_scope_inheritance(self):
        from agent.tools import SubmitAnalysisProgramInput

        request = SubmitAnalysisProgramInput.model_validate({
            'base_execution_id': 17,
            'patches': [{'start_line': 2, 'end_line': 2, 'replacement': 'value = 1'}],
        })
        self.assertEqual(request.code, '')
        self.assertEqual(request.used_datasource_ids, [])
        self.assertEqual(request.patches[0].replacement, 'value = 1')

    def test_isolated_helper_signature_failure_authorizes_verified_statement_patch(self):
        from agent.tools import _validate_runtime_helper_signature_contract

        source = 'report = "Summary"\nresult = save_analysis_result(analysis_report=report, used_datasource_ids=[7])\n'
        retry = _validate_runtime_helper_signature_contract(ast.parse(source), source)
        self.assertIsNotNone(retry)
        message = _tool_error_message(
            tool_call_id='submit', execution_id=17, error_type=retry.retry_type,
            error_message=retry.message, repair_instructions=retry.repair_instructions,
            diagnostics=retry.diagnostics, code_error_context=retry.code_error_context,
            current_failed_code=source,
        )
        payload = json.loads(message.content)
        target = payload['repair_target']
        self.assertEqual(target['base_execution_id'], 17)
        self.assertEqual([(r['start_line'], r['end_line']) for r in target['allowed_ranges']], [(2, 2)])
        self.assertEqual(target['source_sha256'], _build_analysis_logic_signature(source))

    def test_combined_contract_failures_do_not_authorize_signature_only_patch(self):
        from agent.tools import _merge_static_contract_retries, _validate_runtime_helper_signature_contract, request_retry

        source = 'result = save_analysis_result(used_datasource_ids=[7])\n'
        signature = _validate_runtime_helper_signature_contract(ast.parse(source), source)
        combined = _merge_static_contract_retries([
            signature,
            request_retry(retry_type='contract_error', message='Missing query constraints'),
        ])
        message = _tool_error_message(
            tool_call_id='submit', execution_id=17, error_type=combined.retry_type,
            error_message=combined.message, repair_instructions=combined.repair_instructions,
            diagnostics=combined.diagnostics, code_error_context=combined.code_error_context,
            current_failed_code=source,
        )
        self.assertNotIn('repair_target', json.loads(message.content))

    def test_rejected_patch_keeps_immutable_base_authorization_in_feedback(self):
        source = 'value = query(\n    field="amount",\n)\nresult = value.sum()\n'
        target = {
            'location_domain': 'python_source', 'base_execution_id': 17,
            'mode': 'line_patch', 'source_sha256': _build_analysis_logic_signature(source),
            'line_count': 4,
            'allowed_ranges': [{'start_line': 1, 'end_line': 3, 'source': source.split('result')[0].rstrip()}],
        }
        base = SimpleNamespace(
            execution_status='failed', generated_code=source,
            used_datasource_ids_json='[3]',
            result_payload_json=json.dumps({'repair_target': target}),
        )
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = base
        with patch('config.database.SessionLocal', return_value=session):
            with self.assertRaises(SourcePatchResolutionError) as caught:
                _resolve_execute_python_source(
                    conversation_id=1, turn_id=2, code='', used_datasource_ids=[],
                    base_execution_id=17,
                    patches=[SourceCodePatch(start_line=2, end_line=2, replacement='field="other",')],
                )
        error = caught.exception
        self.assertEqual(error.repair_target, target)
        runtime = SimpleNamespace(context=SimpleNamespace(conversation_id=1, turn_id=2), tool_call_id='patch')
        with (
            patch('agent.tools._resolve_execute_python_source', side_effect=error),
            patch('agent.tools._raise_if_runtime_task_cancelled'),
            patch('agent.tools._get_stream_emitter'),
            patch('agent.tools._create_execution_record') as create,
        ):
            message = execute_python.func(runtime=runtime, base_execution_id=17)
        payload = json.loads(message.content)
        self.assertEqual(payload['repair_target'], target)
        self.assertFalse(payload['diagnostics']['new_execution_created'])
        create.assert_not_called()

    def test_rejected_patch_cannot_retain_authorization_for_another_source(self):
        message = _tool_error_message(
            tool_call_id='patch', execution_id=17, error_type='contract_error',
            error_message='patch rejected', repair_instructions=[], current_failed_code='value = 1',
            retained_repair_target={
                'mode': 'line_patch', 'base_execution_id': 17,
                'source_sha256': _build_analysis_logic_signature('value = 2'),
                'allowed_ranges': [{'start_line': 1, 'end_line': 1}],
            },
        )
        self.assertNotIn('repair_target', json.loads(message.content))

    def test_full_repair_can_name_its_failed_parent_without_becoming_a_patch(self):
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            execution_status='failed',
        )
        with patch('config.database.SessionLocal', return_value=session):
            resolved = _resolve_execute_python_source(
                conversation_id=1, turn_id=2, code='result = save_analysis_result()',
                used_datasource_ids=[7], base_execution_id=17, patches=[],
            )
        self.assertEqual(resolved, ('result = save_analysis_result()', [7], 17, 'llm_generated'))
        session.close.assert_called_once()

    def test_full_repair_parent_must_belong_to_the_current_turn(self):
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        with patch('config.database.SessionLocal', return_value=session):
            with self.assertRaisesRegex(ValueError, '不属于当前会话轮次'):
                _resolve_execute_python_source(
                    conversation_id=1, turn_id=2, code='result = save_analysis_result()',
                    used_datasource_ids=[7], base_execution_id=17, patches=[],
                )
        session.close.assert_called_once()

    def test_unavailable_patch_authorization_returns_only_full_program_guidance(self):
        runtime = SimpleNamespace(
            context=SimpleNamespace(conversation_id=1, turn_id=2),
            tool_call_id='patch',
        )
        with (
            patch('agent.tools._resolve_execute_python_source',
                  side_effect=FullProgramRequiredError('Full program required')),
            patch('agent.tools._raise_if_runtime_task_cancelled'),
            patch('agent.tools._get_stream_emitter'),
            patch('agent.tools._create_execution_record') as create,
        ):
            message = execute_python.func(runtime=runtime, base_execution_id=17)
        payload = json.loads(message.content)
        self.assertIn('base_execution_id=0', payload['repair_instructions'][0])
        self.assertIn('patches=[]', payload['repair_instructions'][0])
        self.assertNotIn('start_line', ' '.join(payload['repair_instructions']))
        create.assert_not_called()

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

    def test_review_anchor_resolves_rendered_f_string_to_owning_statement(self):
        source = (
            'manufacturer = "ABB"\n'
            'count = 1\n'
            'share = 10.0\n'
            'report_lines.append(f"**{manufacturer}** 对应设备最多，为 **{count}** 台，占比 {share:.1f}%")\n'
        )

        target = _review_source_patch_range(
            source,
            '**ABB** 对应设备最多，为 **1** 台，占比 10.0%',
        )

        self.assertIsNotNone(target)
        self.assertEqual((target['start_line'], target['end_line']), (4, 4))
        self.assertIn('report_lines.append', target['source'])

    def test_presentation_revision_can_target_complete_report_assignment_without_snippet(self):
        source = (
            'value = 1\n'
            'analysis_report = f"""\n'
            '## Summary\n'
            'Value: {value}\n'
            '"""\n'
            'result = save_analysis_result(analysis_report=analysis_report)\n'
        )

        target = _named_assignment_patch_range(source, 'analysis_report')

        self.assertEqual((target['start_line'], target['end_line']), (2, 5))
        self.assertEqual(target['resolved_by'], 'named_output_assignment')
        self.assertIn('Value: {value}', target['source'])

    def test_presentation_review_without_anchor_uses_report_assignment_patch(self):
        source = (
            'value = 1\n'
            'analysis_report = f"""## Summary\nValue: {value}\n"""\n'
            'result = save_analysis_result(analysis_report=analysis_report)\n'
        )
        candidate = SimpleNamespace(id=17, generated_code=source)
        runtime = SimpleNamespace(
            context=SimpleNamespace(conversation_id=1, turn_id=2),
            tool_call_id='review-patch',
        )
        with (
            patch('agent.tools._get_stream_emitter', return_value=MagicMock()),
            patch('agent.tools._latest_successful_review_execution', return_value=candidate),
            patch('agent.tools._persist_review_revision_target') as persist_target,
        ):
            message = request_analysis_revision.func(
                runtime=runtime,
                issues=[AnalysisRevisionIssue(
                    issue_type='presentation',
                    description='Summary wording overstates the comparison.',
                    required_correction='State the verified comparison only.',
                    change_scope='presentation',
                )],
            )

        payload = json.loads(message.content)
        packet = payload['revision_work_packet']
        self.assertTrue(packet['patch_mode_allowed'])
        self.assertFalse(packet['full_program_revision_required'])
        self.assertEqual(
            [(item['start_line'], item['end_line']) for item in packet['allowed_ranges']],
            [(2, 4)],
        )
        persist_target.assert_called_once()

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

    def test_review_patch_keeps_successful_baseline_after_a_failed_revision_attempt(self):
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
        base_query = session.query.return_value.filter.return_value
        base_query.first.return_value = base
        successful_query = base_query.order_by.return_value
        successful_query.first.return_value = base

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

        latest_success_filters = session.query.return_value.filter.call_args_list[-1].args
        self.assertTrue(any('execution_status' in str(item) for item in latest_success_filters))
        self.assertEqual(repaired, 'value = 1\nreport = f"corrected={value}"\n')
        self.assertEqual(ids, [3])
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

    def test_syntax_patch_range_feedback_distinguishes_authorization_from_ast_boundary(self):
        source = 'value = items[\n# nearby explanation\nresult = 1\n'
        with self.assertRaisesRegex(ValueError, '请求区间 1-2.*可用区间：1-1') as captured:
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=1, end_line=2, replacement='value = items')],
                authorized_ranges={(1, 1)},
            )
        self.assertIsInstance(captured.exception.__cause__, SyntaxError)
        self.assertNotIn('不能从字典', str(captured.exception))
        self.assertIn('完整 code', str(captured.exception))
        repaired = _apply_source_code_patches(
            source,
            [SourceCodePatch(start_line=1, end_line=1, replacement='value = items')],
            authorized_ranges={(1, 1)},
        )
        self.assertEqual(repaired, 'value = items\n# nearby explanation\nresult = 1\n')

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
                'allowed_ranges': [{'start_line': 1, 'end_line': 1}],
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
        self.assertFalse(payload['minimal_patch_required'])

    def test_multiline_string_syntax_error_keeps_context_without_authorizing_closing_line_patch(self):
        code = 'execution_intent = "analysis"\ntext = f"""Report\n{values[\'amount]}\n"""\n'
        retry = _validate_generated_code_contract(code)
        self.assertIsNotNone(retry)
        assert retry is not None
        context = retry.code_error_context
        self.assertEqual([item['line_no'] for item in context['context_lines']], [2, 3, 4])
        message = _tool_error_message(
            tool_call_id='multi-syntax', execution_id=679,
            error_type=retry.retry_type, error_message=retry.message,
            repair_instructions=retry.repair_instructions, diagnostics=retry.diagnostics,
            code_error_context=context, current_failed_code=code,
        )
        payload = json.loads(message.content)
        self.assertNotIn('repair_target', payload)
        self.assertEqual(payload['current_failed_code'], code)
        self.assertFalse(payload['minimal_patch_required'])

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
