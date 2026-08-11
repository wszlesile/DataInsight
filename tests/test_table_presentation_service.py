import unittest
from unittest.mock import Mock, patch

from service.table_presentation_service import normalize_table_column_labels


class TablePresentationServiceTest(unittest.TestCase):
    def test_uses_schema_business_name_without_model_call(self) -> None:
        tables = [{
            "title": "Batch details",
            "columns": [
                {"key": "batch_id", "label": "batch_id"},
                {"key": "quality_status", "label": "quality_status"},
            ],
            "rows": [{"batch_id": "B-1", "quality_status": "near_limit"}],
        }]
        snapshot = [{
            "metadata_schema": {
                "properties": {
                    "batch_id": {"description": "Production batch; identifier"},
                    "quality_status": {"displayName": "Quality status"},
                }
            }
        }]

        with patch(
            "service.table_presentation_service._invoke_label_resolver"
        ) as resolver:
            result = normalize_table_column_labels(tables, snapshot)

        resolver.assert_not_called()
        self.assertEqual(result.local_count, 2)
        self.assertEqual(result.model_count, 0)
        self.assertEqual(result.unresolved, [])
        self.assertEqual(
            [column["label"] for column in result.tables[0]["columns"]],
            ["Production batch", "Quality status"],
        )
        self.assertEqual(tables[0]["columns"][0]["label"], "batch_id")

    def test_resolves_only_derived_columns_with_compact_model_call(self) -> None:
        tables = [{
            "title": "Root cause coverage",
            "description": "Evidence coverage by candidate.",
            "columns": [
                {"key": "candidate", "label": "candidate"},
                {"key": "evidence_batches", "label": "evidence_batches"},
            ],
            "rows": [{"candidate": "Process variation", "evidence_batches": 7}],
        }]
        resolved = [
            {
                "table_index": 0,
                "column_index": 0,
                "key": "candidate",
                "label": "Root cause candidate",
            },
            {
                "table_index": 0,
                "column_index": 1,
                "key": "evidence_batches",
                "label": "Covered batches",
            },
        ]

        with patch(
            "service.table_presentation_service._invoke_label_resolver",
            return_value=resolved,
        ) as resolver:
            result = normalize_table_column_labels(tables, [])

        resolver.assert_called_once()
        self.assertEqual(result.local_count, 0)
        self.assertEqual(result.model_count, 2)
        self.assertEqual(result.unresolved, [])
        self.assertEqual(
            [column["label"] for column in result.tables[0]["columns"]],
            ["Root cause candidate", "Covered batches"],
        )

    def test_leaves_unresolved_labels_for_existing_contract_fallback(self) -> None:
        tables = [{
            "title": "Derived table",
            "columns": [{"key": "derived_score", "label": "derived_score"}],
            "rows": [{"derived_score": 1.2}],
        }]

        with patch(
            "service.table_presentation_service._invoke_label_resolver",
            return_value=[],
        ):
            result = normalize_table_column_labels(tables, [])

        self.assertEqual(result.model_count, 0)
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(
            result.tables[0]["columns"][0]["label"],
            "derived_score",
        )


if __name__ == "__main__":
    unittest.main()
