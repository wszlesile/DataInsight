from datetime import datetime
from zoneinfo import ZoneInfo

from agent.tools import _time_series_coverage_context
from agent.runtime_working_context import _compact_query_fact


def test_time_series_coverage_records_future_end_and_partial_final_day():
    coverage = _time_series_coverage_context(
        {
            'time_range': {
                'start': '2026-10-03T00:00:00+08:00',
                'end': '2026-10-10T00:00:00+08:00',
                'end_exclusive': True,
            },
            'time_series_query': {'mode': 'aggregate', 'window': '1 hour'},
        },
        as_of=datetime(2026, 10, 9, 14, 45, tzinfo=ZoneInfo('Asia/Shanghai')),
    )

    assert coverage['basis'] == 'requested_range_vs_execution_time'
    assert coverage['requested_end_after_execution'] is True
    assert coverage['window_seconds'] == 3600
    assert coverage['partial_calendar_days'] == [{
        'date': '2026-10-09',
        'coverage_start': '2026-10-09T00:00:00+08:00',
        'coverage_end_at_execution': '2026-10-09T14:45:00+08:00',
        'covered_seconds_at_execution': 14 * 3600 + 45 * 60,
        'period_seconds': 24 * 3600,
        'state': 'partial_at_execution_time',
    }]
    assert coverage['data_completeness'] == 'not_inferred_from_time_coverage'


def test_compact_query_fact_exposes_execution_temporal_coverage():
    coverage = {'requested_end_after_execution': True, 'partial_calendar_days': [{'date': '2026-10-09'}]}
    fact = _compact_query_fact({
        'query_key': 'hourly',
        'query_constraints': {'time_series_query': {'window': '1 hour'}},
        'execution': {'status': 'success', 'row_count': 159, 'temporal_coverage': coverage},
    })

    assert fact['execution']['temporal_coverage'] == coverage


def test_time_series_coverage_marks_an_explicit_midday_end_as_partial():
    coverage = _time_series_coverage_context(
        {
            'time_range': {
                'start': '2026-10-03T00:00:00+08:00',
                'end': '2026-10-09T14:45:00+08:00',
                'end_exclusive': True,
            },
            'time_series_query': {'mode': 'aggregate', 'window': '1 hour'},
        },
        as_of=datetime(2026, 10, 10, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai')),
    )

    assert coverage['requested_end_after_execution'] is False
    assert coverage['partial_calendar_days'][-1]['date'] == '2026-10-09'
    assert coverage['partial_calendar_days'][-1]['state'] == 'partial_by_requested_range'
