import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import tools


def test_feedback_lists_all_derived_scope_errors_without_inventing_replacements():
    code = '''
q1 = "SELECT AVG(amount) FROM (SELECT AVG(amount) AS mean_amount FROM sales) b"
q2 = "WITH b AS (SELECT SUM(amount) AS total FROM sales) SELECT amount FROM b"
'''
    result = tools._build_derived_query_projection_diagnostics(code)
    errors = result['derived_query_projection_mismatches']
    assert len(errors) == 2
    assert [item['missing_projection_fields'] for item in errors] == [['amount'], ['amount']]
    assert [item['available_projection_fields'] for item in errors] == [['mean_amount'], ['total']]


@pytest.mark.parametrize('sql', [
    'SELECT AVG(mean_amount) FROM (SELECT AVG(amount) AS mean_amount FROM sales) b',
    'SELECT amount FROM sales',
    'SELECT amount FROM (SELECT * FROM sales) b',
    'SELECT amount FROM (SELECT id FROM sales) b JOIN items i ON i.id = b.id',
    'WITH b(value) AS (SELECT amount FROM sales) SELECT value FROM b',
    'SELECT mean_amount AS value FROM (SELECT AVG(amount) AS mean_amount FROM sales) b ORDER BY value',
    'SELECT amount FROM (SELECT amount FROM sales UNION ALL SELECT value FROM returns) b',
    'SELECT amount FROM sales t JOIN returns t ON t.id = t.id',
])
def test_feedback_does_not_guess_opaque_or_valid_scope(sql):
    assert tools._build_derived_query_projection_diagnostics(f'q = {sql!r}') == {}


def test_diagnostics_are_added_only_after_a_column_failure():
    result = tools._augment_schema_field_mismatch_feedback(
        code='q = "SELECT amount FROM (SELECT SUM(amount) AS total FROM sales) b"',
        error_type='schema_or_column_mismatch', error_message="Column 'amount' cannot be resolved",
        error_signature={}, diagnostics={}, repair_instructions=[],
        code_error_context={}, selected_datasource_snapshot=[],
    )
    assert result[2]['derived_query_projection_mismatches']
    assert any('SQL alias/scope' in item for item in result[3])
