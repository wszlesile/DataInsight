import json
import unittest
from types import SimpleNamespace

from agent.invoker import _get_latest_failed_repair_target
from agent.tools import (
    ExePythonCodeInput,
    SourceCodePatch,
    _apply_source_code_patches,
    _tool_error_message,
)


class RuntimeSourcePatchRepairTest(unittest.TestCase):
    def test_patch_changes_only_the_addressed_lines(self):
        source = "a = 1\nb = 2\nc = a + b\nprint(c)\n"

        repaired = _apply_source_code_patches(
            source,
            [SourceCodePatch(start_line=2, end_line=3, replacement="b = 3\nc = a * b")],
        )

        self.assertEqual(repaired, "a = 1\nb = 3\nc = a * b\nprint(c)\n")

    def test_patch_rejects_overlapping_ranges(self):
        with self.assertRaisesRegex(ValueError, "不能包含重叠"):
            _apply_source_code_patches(
                "a\nb\nc\nd\n",
                [
                    SourceCodePatch(start_line=2, end_line=3, replacement="x"),
                    SourceCodePatch(start_line=3, end_line=4, replacement="y"),
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
        )
        payload = json.loads(message.content)

        self.assertEqual(payload["repair_target"]["base_execution_id"], 676)
        self.assertEqual(payload["repair_target"]["line_count"], 2)
        self.assertEqual(payload["repair_target"]["mode"], "line_patch")

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
