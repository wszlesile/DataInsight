import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from agent.tools import _infer_sql_aggregate_numeric_columns


@pytest.mark.parametrize('sql', [
    'SELECT MAX(n) AS peak FROM (SELECT COUNT(*) AS n FROM t GROUP BY k) x',
    'WITH x AS (SELECT SUM(CASE WHEN flag=1 THEN 1 ELSE 0 END) AS n '
    'FROM t GROUP BY k) SELECT MAX(n) AS peak FROM x',
    'WITH x AS (SELECT COUNT(*) AS n FROM t GROUP BY k), '
    'y AS (SELECT n AS amount FROM x) SELECT MIN(amount) AS peak FROM y',
])
def test_derived_numeric_types_reach_outer_result(sql):
    assert 'peak' in _infer_sql_aggregate_numeric_columns(sql)


@pytest.mark.parametrize('sql', [
    'SELECT MAX(name) AS peak FROM t',
    "SELECT MAX(n) AS peak FROM (SELECT '001' AS n) x",
    "SELECT MAX(n) AS peak FROM (SELECT CAST('2026-01-01' AS DATE) AS n) x",
    'SELECT MAX(x.n) AS peak FROM t x JOIN '
    '(SELECT COUNT(*) AS n FROM u) y ON x.id=y.n',
])
def test_text_date_and_unresolved_columns_are_not_coerced(sql):
    assert 'peak' not in _infer_sql_aggregate_numeric_columns(sql)
