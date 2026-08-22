from copy import deepcopy
from datetime import datetime

import pytest

from analysis_runtime.parameters import (
    attach_relative_time_runtime_default,
    build_reusable_parameter_defaults,
    materialize_analysis_parameters,
    strip_generated_runtime_parameter_metadata,
)
from agent.tools import CURRENT_EXPECTED_TIME_RANGE, save_analysis_result


def _schema():
    return {
        'parameters': [{
            'name': 'time_range',
            'type': 'time_range',
            'required': True,
            'runtime_default': {
                'kind': 'relative_time_range',
                'amount': 10,
                'unit': 'minute',
                'anchor': 'execution_time',
                'timezone': 'Asia/Shanghai',
                'end_exclusive': True,
            },
        }],
        'defaults': {
            'time_range': {
                'start': '2026-08-19T17:49:56+08:00',
                'end': '2026-08-19T17:59:56+08:00',
                'end_exclusive': True,
            },
        },
    }


def test_reusable_defaults_preserve_relative_time_binding():
    defaults = build_reusable_parameter_defaults(
        _schema(),
        {
            'time_range': {
                'start': '2026-08-19T17:49:56+08:00',
                'end': '2026-08-19T17:59:56+08:00',
                'end_exclusive': True,
            },
        },
    )

    assert defaults['time_range']['kind'] == 'relative_time_range'
    assert defaults['time_range']['amount'] == 10


def test_relative_time_range_materializes_from_logical_trigger_time():
    values = build_reusable_parameter_defaults(_schema(), {})

    materialized = materialize_analysis_parameters(
        _schema(),
        values,
        anchor_time=datetime(2026, 8, 21, 10, 12, 0),
    )

    assert materialized['time_range'] == {
        'start': '2026-08-21T18:02:00+08:00',
        'end': '2026-08-21T18:12:00+08:00',
        'end_exclusive': True,
    }


def test_calendar_day_relative_range_preserves_natural_day_count():
    schema = _schema()
    schema['parameters'][0]['runtime_default'] = {
        'kind': 'relative_time_range',
        'amount': 7,
        'unit': 'day',
        'anchor': 'execution_time',
        'timezone': 'Asia/Shanghai',
        'alignment': 'calendar_day',
        'end_exclusive': True,
    }
    values = build_reusable_parameter_defaults(schema, {})

    materialized = materialize_analysis_parameters(
        schema,
        values,
        anchor_time=datetime(2026, 8, 22, 10, 12, 0),
    )

    assert materialized['time_range'] == {
        'start': '2026-08-16T00:00:00+08:00',
        'end': '2026-08-23T00:00:00+08:00',
        'end_exclusive': True,
    }


def test_explicit_absolute_time_range_overrides_relative_default():
    explicit = {
        'time_range': {
            'start': '2026-07-01T00:00:00+08:00',
            'end': '2026-07-02T00:00:00+08:00',
            'end_exclusive': True,
        },
    }

    assert materialize_analysis_parameters(
        _schema(),
        explicit,
        anchor_time=datetime(2026, 8, 21, 10, 12, 0),
    )['time_range'] == explicit['time_range']


def test_expected_relative_range_enriches_matching_schema_without_mutating_input():
    schema = _schema()
    schema['parameters'][0].pop('runtime_default')
    concrete = schema['defaults']
    enriched = attach_relative_time_runtime_default(
        schema,
        expected_time_range={
            'start': '2026-08-19T17:49:56+08:00',
            'end': '2026-08-19T17:59:56+08:00',
            'relative_window': True,
            'amount': 10,
            'unit': '分钟',
            'end_exclusive': True,
        },
        concrete_values=concrete,
    )

    assert 'runtime_default' not in schema['parameters'][0]
    assert enriched['parameters'][0]['runtime_default']['unit'] == 'minute'
    assert enriched['parameters'][0]['runtime_default']['end_exclusive'] is True


def test_generated_runtime_metadata_is_removed_from_schema_defaults_and_values():
    runtime_default = {
        'kind': 'relative_time_range',
        'amount': 10,
        'unit': 'minute',
        'anchor': 'execution_time',
        'timezone': 'Asia/Shanghai',
        'end_exclusive': False,
    }
    schema = {
        'parameters': [{
            'name': 'time_range',
            'type': 'time_range',
            'required': True,
            'runtime_default': runtime_default,
        }],
        'defaults': {
            'time_range': {
                'start': '2026-08-22T00:00:00+08:00',
                'end': '2026-08-22T00:10:00+08:00',
                'end_exclusive': False,
                'runtime_default': runtime_default,
            },
        },
    }
    values = deepcopy(schema['defaults'])

    clean_schema, clean_values = strip_generated_runtime_parameter_metadata(schema, values)

    assert 'runtime_default' not in clean_schema['parameters'][0]
    assert 'runtime_default' not in clean_schema['defaults']['time_range']
    assert 'runtime_default' not in clean_values['time_range']
    assert 'runtime_default' in schema['parameters'][0]
    assert 'runtime_default' in values['time_range']


def test_fixed_time_range_remains_fixed_without_relative_semantics():
    schema = _schema()
    schema['parameters'][0].pop('runtime_default')
    concrete = schema['defaults']

    enriched = attach_relative_time_runtime_default(
        schema,
        expected_time_range={
            'start': '2026-08-19T17:49:56+08:00',
            'end': '2026-08-19T17:59:56+08:00',
            'relative_window': False,
        },
        concrete_values=concrete,
    )

    assert 'runtime_default' not in enriched['parameters'][0]
    assert materialize_analysis_parameters(
        enriched,
        concrete,
        anchor_time=datetime(2026, 8, 21, 10, 12, 0),
    )['time_range'] == concrete['time_range']


def test_invalid_relative_time_binding_is_rejected_before_execution():
    values = build_reusable_parameter_defaults(_schema(), {})
    values['time_range']['unit'] = 'fortnight'

    with pytest.raises(ValueError, match='单位不受支持'):
        materialize_analysis_parameters(_schema(), values)


def test_analysis_result_persists_relative_runtime_semantics_from_context():
    schema = _schema()
    schema['parameters'][0].pop('runtime_default')
    concrete = schema['defaults']
    token = CURRENT_EXPECTED_TIME_RANGE.set({
        'start': '2026-08-19T17:49:56+08:00',
        'end': '2026-08-19T17:59:56+08:00',
        'relative_window': True,
        'amount': 10,
        'unit': '分钟',
        'end_exclusive': True,
    })
    try:
        result = save_analysis_result(
            analysis_report='近十分钟趋势正常。',
            analysis_parameters_schema=schema,
            analysis_parameters=concrete,
        )
    finally:
        CURRENT_EXPECTED_TIME_RANGE.reset(token)

    runtime_default = result.analysis_parameters_schema['parameters'][0]['runtime_default']
    assert runtime_default['kind'] == 'relative_time_range'
    assert runtime_default['amount'] == 10


def test_analysis_result_does_not_persist_generated_runtime_metadata_in_values():
    schema = _schema()
    schema['defaults']['time_range']['runtime_default'] = deepcopy(
        schema['parameters'][0]['runtime_default']
    )
    concrete = deepcopy(schema['defaults'])
    token = CURRENT_EXPECTED_TIME_RANGE.set({
        'start': '2026-08-19T17:49:56+08:00',
        'end': '2026-08-19T17:59:56+08:00',
        'relative_window': True,
        'amount': 10,
        'unit': '分钟',
        'end_exclusive': True,
    })
    try:
        result = save_analysis_result(
            analysis_report='近十分钟趋势正常。',
            analysis_parameters_schema=schema,
            analysis_parameters=concrete,
        )
    finally:
        CURRENT_EXPECTED_TIME_RANGE.reset(token)

    assert 'runtime_default' not in result.analysis_parameters['time_range']
    assert 'runtime_default' not in result.analysis_parameters_schema['defaults']['time_range']
    assert result.analysis_parameters_schema['parameters'][0]['runtime_default']['amount'] == 10
