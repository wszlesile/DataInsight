"""Render local facts and optional caller-authored interpretations."""

from string import Formatter
from typing import Any, Literal

import pandas as pd

from analysis_runtime.calculations import evaluate_numeric_comparison, summarize_extrema


class _ValueFormatter(Formatter):
    def __init__(self, unavailable: str):
        self.unavailable = unavailable

    def format_field(self, value: Any, format_spec: str) -> str:
        return self.unavailable if value is None else super().format_field(value, format_spec)

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        return None if value is None else super().convert_field(value, conversion)


def _render_case(state: str, cases: dict[str, str], values: dict[str, Any], unavailable: str, field_spans=None) -> str:
    # Validate the whole interface, but format only the selected case. Missing
    # inputs in an unselected branch must not cause a formatting failure.
    for template in cases.values():
        if not isinstance(template, str) or not template.strip():
            raise ValueError('Every result case requires a non-empty expression string')
        for _, field, spec, _ in Formatter().parse(template):
            if field is None:
                continue
            if field not in values:
                raise ValueError(f'Unknown expression field: {field!r}')
            if '{' in spec or '}' in spec:
                raise ValueError('Nested expression format fields are not supported')
    formatter = _ValueFormatter(unavailable)
    if field_spans is None:
        return formatter.vformat(cases[state], (), values)
    rendered = ''
    for literal, field, spec, conversion in Formatter().parse(cases[state]):
        rendered += literal
        if field is not None:
            value = formatter.convert_field(values[field], conversion) if conversion else values[field]
            text = formatter.format_field(value, spec)
            field_spans.append({'field': field, 'start': len(rendered), 'end': len(rendered) + len(text),
                                'value_format': spec, 'conversion': conversion})
            rendered += text
    return rendered


def render_numeric_comparison(
    left: Any, right: Any, *, less: str, equal: str, greater: str, missing: str,
    left_label: str = '', right_label: str = '',
    unavailable: str = 'N/A',
) -> str:
    """Choose one of four expressions using the actual numeric relation.

    Fields: left, right, difference, relative_change, left_label, right_label.
    relative_change is a fraction; format .1% displays a percentage. Missing
    or zero-base relative change remains None. Text is caller-authored; this
    function cannot certify that a business assessment has a valid criterion.
    Undefined values render as unavailable, including zero-base changes.
    """
    result = evaluate_numeric_comparison(left, right)
    values = {key: result[key] for key in ('left', 'right', 'difference', 'relative_change')}
    values.update(left_label=left_label, right_label=right_label)
    state = result['relation'] or 'missing'
    return _render_case(state, {'less': less, 'equal': equal, 'greater': greater, 'missing': missing}, values, unavailable)


def _extrema_fact_templates(mode: str, language: str) -> tuple[dict[str, str], str]:
    """Canonical literal templates shared with the non-executing Evidence compiler."""
    if language not in {'zh', 'en'}:
        raise ValueError("language must be 'zh' or 'en'")
    if language == 'zh':
        extreme = '最高' if mode == 'max' else '最低'
        defaults = {
            'empty': '没有可比较的{value_label}记录。',
            'single': '仅有 {labels} 一个可比较项，{value_label}为 {value:g}{unit}，无法作项间比较。',
            'unique': '在 {valid_count} 个可比较项中，{labels}的{value_label}' + extreme + '，为 {value:g}{unit}。',
            'tied': '在 {valid_count} 个可比较项中，{labels}的{value_label}并列' + extreme + '，各为 {value:g}{unit}。',
            'all_equal': '{valid_count} 个可比较项的{value_label}均为 {value:g}{unit}：{labels}。',
        }
        missing = '另有 {missing_count} 条记录的{value_label}不可比较。'
    else:
        extreme = 'highest' if mode == 'max' else 'lowest'
        defaults = {
            'empty': 'No comparable {value_label} records.',
            'single': 'Only {labels} is comparable: {value_label} {value:g}{unit}; no between-item comparison.',
            'unique': 'Among {valid_count} comparable items, {labels} has the ' + extreme + ' {value_label}: {value:g}{unit}.',
            'tied': 'Among {valid_count} comparable items, {labels} are tied for the ' + extreme + ' {value_label}: {value:g}{unit} each.',
            'all_equal': 'All {valid_count} comparable items have {value_label} {value:g}{unit}: {labels}.',
        }
        missing = '{missing_count} additional records have no comparable {value_label}.'
    return defaults, missing


def render_extrema(
    data: pd.DataFrame, value_field: str, label_field: str, *, empty: str | None = None,
    single: str | None = None, unique: str | None = None, tied: str | None = None,
    all_equal: str | None = None, mode: str = 'max',
    separator: str = ', ', unavailable: str = 'N/A',
    value_label: str = '', unit: str = '', language: Literal['zh', 'en'] = 'zh',
) -> str:
    """Render the complete extreme set without dropping ties or equality.

    Without templates, emit a factual comparison of the full input using all
    matching labels. Each input row is one comparable item, not necessarily
    a distinct business entity. Units describe the computed value; no risk,
    concentration or health judgement is inferred. Missing values are disclosed.

    Fields: labels, value, valid_count, missing_count, matched_count. Every
    non-empty state has labels/value; empty input has no value. Counts concern
    comparable rows, not entities. Optional expressions override the selected
    mathematical case; their business interpretation remains caller-authored.
    """
    result = summarize_extrema(data, value_field, mode=mode)
    return _render_extrema_summary(
        result, label_field, empty=empty, single=single, unique=unique,
        tied=tied, all_equal=all_equal, separator=separator, unavailable=unavailable,
        value_label=value_label or value_field, unit=unit, language=language,
    )


def _render_extrema_summary(
    result: dict[str, Any], label_field: str, *, empty: str | None = None,
    single: str | None = None, unique: str | None = None, tied: str | None = None,
    all_equal: str | None = None, separator: str = ', ', unavailable: str = 'N/A',
    value_label: str = '', unit: str = '', language: Literal['zh', 'en'] = 'zh', field_spans=None,
) -> str:
    """Format an already evaluated comparison without recalculating its inputs.

    Internal interface for the execution ledger; caller-authored interpretation
    remains uncertified, just as with render_extrema.
    """
    defaults, missing = _extrema_fact_templates(result['mode'], language)
    labels = result['rows'][label_field]
    if not isinstance(labels, pd.Series):
        raise ValueError('label_field must identify a single column')
    values = {
        'labels': separator.join(str(label) for label in labels),
        'value': result['extreme_value'],
        'value_label': value_label, 'unit': unit,
        **{key: result[key] for key in ('valid_count', 'missing_count', 'matched_count')},
    }
    overrides = {
        'empty': empty, 'single': single, 'unique': unique,
        'tied': tied, 'all_equal': all_equal,
    }
    rendered = _render_case(result['kind'], {
        key: defaults[key] if template is None else template for key, template in overrides.items()
    }, values, unavailable, field_spans=field_spans)
    if result['missing_count'] and overrides[result['kind']] is None:
        rendered += ' ' + _ValueFormatter(unavailable).vformat(missing, (), values)
    return rendered
