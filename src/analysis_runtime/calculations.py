"""Numerical facts for generated programs, without business interpretations."""

from decimal import Decimal
import math
from numbers import Real
from typing import Any, Literal

import pandas as pd


def _finite_number(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise TypeError('Expected a real numeric value, not a label or boolean')
    if isinstance(value, Decimal):
        if value.is_nan():
            return None
        finite = value.is_finite()
    elif isinstance(value, int):
        finite = True
    else:
        if math.isnan(value):
            return None
        finite = math.isfinite(value)
    if not finite:
        raise ValueError('Infinite values cannot establish a finite comparison or ratio')
    return value


def select_extreme_rows(
    data: pd.DataFrame, value_field: str, *, mode: Literal['max', 'min'] = 'max',
) -> pd.DataFrame:
    """Return all rows equal to the extremum, preserving columns and row order.

    Missing values do not participate. Empty/all-missing input returns an empty
    frame with the same columns. Equality is exact; round explicitly beforehand
    when the analysis defines a comparison precision. No ranking tie is dropped.
    """
    if mode not in {'max', 'min'}:
        raise ValueError('mode must be max or min')
    if not isinstance(data, pd.DataFrame):
        raise TypeError('data must be a DataFrame')
    values = data[value_field]
    if not isinstance(values, pd.Series):
        raise ValueError('value_field must identify a single column')
    checked = [_finite_number(value) for value in values]
    observed = [value for value in checked if value is not None]
    if not observed:
        return data.iloc[:0].copy()
    extreme = max(observed) if mode == 'max' else min(observed)
    mask = [value is not None and value == extreme for value in checked]
    return data.loc[mask].copy()


def compare_numeric_values(left: Any, right: Any) -> str | None:
    """Return less/equal/greater, or None when either operand is missing.

    This is a numerical relation, not an assessment of risk or adequacy.
    """
    left, right = _finite_number(left), _finite_number(right)
    if left is None or right is None:
        return None
    if left == right:
        return 'equal'
    return 'less' if left < right else 'greater'


def calculate_ratio(numerator: Any, denominator: Any, *, scale: Any = 1) -> Any:
    """Compute numerator / denominator * scale without rounding.

    Missing operands or a zero denominator return None, never a fabricated zero.
    Scale 1 returns a fraction; scale 100 returns a percentage number. Operand
    units/population and whether a ratio is meaningful belong to the analysis.
    """
    numerator = _finite_number(numerator)
    denominator = _finite_number(denominator)
    scale = _finite_number(scale)
    if scale is None:
        raise ValueError('scale must be a finite number')
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _finite_number(numerator / denominator * scale)


def evaluate_numeric_comparison(left: Any, right: Any) -> dict[str, Any]:
    """Compute a comparison and its deltas once, without a business assessment.

    relative_change is (left - right) / right, not a percentage-point delta.
    Missing operands have unknown predicates; a zero baseline has no relative
    change. Labels, units and external benchmarks remain the caller's inputs.
    """
    left, right = _finite_number(left), _finite_number(right)
    relation = compare_numeric_values(left, right)
    difference = None if relation is None else _finite_number(left - right)
    return {
        'left': left, 'right': right, 'relation': relation,
        'less': None if relation is None else relation == 'less',
        'equal': None if relation is None else relation == 'equal',
        'greater': None if relation is None else relation == 'greater',
        'difference': difference,
        'relative_change': calculate_ratio(difference, right),
    }


def summarize_extrema(
    data: pd.DataFrame, value_field: str, *, mode: Literal['max', 'min'] = 'max',
) -> dict[str, Any]:
    """Return the complete extremum set and its result-dependent ranking state.

    Counts concern comparable rows, not distinct business entities. The result
    does not declare concentration, risk or any other business interpretation.
    """
    rows = select_extreme_rows(data, value_field, mode=mode)
    valid_count = sum(_finite_number(value) is not None for value in data[value_field])
    matched_count = len(rows)
    if not matched_count:
        kind = 'empty'
    elif valid_count == 1:
        kind = 'single'
    elif matched_count == valid_count:
        kind = 'all_equal'
    elif matched_count > 1:
        kind = 'tied'
    else:
        kind = 'unique'
    return {
        'rows': rows, 'kind': kind, 'mode': mode,
        'valid_count': valid_count, 'missing_count': len(data) - valid_count,
        'matched_count': matched_count,
        'extreme_value': None if rows.empty else rows[value_field].iloc[0],
    }
