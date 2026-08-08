import json
import unittest
from types import SimpleNamespace

from agent.invoker import _get_latest_failed_repair_target
from agent.tools import (
    ExePythonCodeInput,
    SourceCodePatch,
    _apply_source_code_patches,
    _extend_query_dependency_ranges,
    _tool_error_message,
)


class RuntimeSourcePatchRepairTest(unittest.TestCase):
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

        self.assertEqual(payload["repair_target"]["base_execution_id"], 676)
        self.assertEqual(payload["repair_target"]["line_count"], 2)
        self.assertEqual(payload["repair_target"]["mode"], "line_patch")
        self.assertEqual(payload["repair_target"]["allowed_ranges"][0]["start_line"], 2)

    def test_patch_rejects_partial_python_statement(self):
        source = "query = (\n    'select * ' \n    'from demo'\n)\nresult = run(query)\n"

        with self.assertRaisesRegex(ValueError, "完整替换一个 Python 语句"):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=2, end_line=3, replacement="'select count(*) from demo'")],
            )

    def test_patch_rejects_syntax_error_before_execution_is_created(self):
        source = "query = 'select * from demo'\nresult = run(query)\n"

        with self.assertRaisesRegex(ValueError, "合成后的完整代码存在语法错误"):
            _apply_source_code_patches(
                source,
                [SourceCodePatch(start_line=1, end_line=1, replacement="query = '''select")],
            )

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
        self.assertEqual(target["code_error_context"]["line_no"], 2)


if __name__ == "__main__":
    unittest.main()
