import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.context_engineering_runtime import _build_datasource_prompt_item  # noqa: E402
from agent.analysis_preparation import (  # noqa: E402
    _normalize_time_series_datasource_snapshot,
)
from agent.tools import (  # noqa: E402
    CURRENT_QUERY_SNAPSHOTS,
    _normalize_time_series_aggregate_result_columns,
    _validate_generated_code_contract,
    validate_query_constraints,
)
from utils.datasource_utils import build_table_runtime_metadata  # noqa: E402


START = "2026-08-19T16:00:00+08:00"
END = "2026-08-19T17:00:00+08:00"
IDENTIFIER = "sensor_history"


def _constraints(
    *,
    mode,
    window=None,
    aggregations=None,
    interpolation=None,
    start=START,
    end=END,
):
    time_series_query = {
        "mode": mode,
        "decision_source": "explicit_user",
    }
    if window is not None:
        time_series_query["window"] = window
    if aggregations is not None:
        time_series_query["aggregations"] = aggregations
    if interpolation is not None:
        time_series_query["interpolation"] = interpolation
    return {
        "time_range": {
            "start": start,
            "end": end,
            "end_exclusive": True,
        },
        "target_filters": [],
        "aggregation": {
            "grain": window or "raw",
            "metric": "value",
        },
        "time_series_query": time_series_query,
    }


def _validate_vxbase(inner_sql, constraints):
    escaped = inner_sql.replace("'", "''")
    return validate_query_constraints(
        query_key="q_vxbase",
        query_constraints=constraints,
        sql=f"SELECT * FROM ts_query('vxbase', '{escaped}')",
        sql_context={
            "fragments": [
                {
                    "kind": "time_series_sql",
                    "ts_query_source": "vxbase",
                    "inner_sql": inner_sql,
                    "datasource_identifier": IDENTIFIER,
                    "time_field": "ts",
                    "quality_field": "status",
                    "value_fields": ["value"],
                }
            ]
        },
    )


def test_vxbase_short_raw_query_remains_supported():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" limit 5000'
    )

    assert _validate_vxbase(inner_sql, _constraints(mode="raw")) is None


def test_persisted_query_without_new_contract_remains_runtime_compatible():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" limit 5000'
    )
    legacy_constraints = {
        "time_range": {"start": START, "end": END, "end_exclusive": True},
        "aggregation": {"grain": "raw", "metric": "value"},
    }

    assert _validate_vxbase(inner_sql, legacy_constraints) is None


def test_vxbase_sampling_rejects_raw_fetch_plus_local_resampling():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" limit 10000'
    )

    retry = _validate_vxbase(
        inner_sql,
        _constraints(mode="sample", window="1min"),
    )

    assert retry is not None
    assert retry.diagnostics["violated_constraint"] == "time_series_native_window"


def test_vxbase_sampling_accepts_native_interval_with_window_alias():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" '
        'interval("1m", "10s") interpolation("previous")'
    )

    assert _validate_vxbase(
        inner_sql,
        _constraints(
            mode="sample",
            window="1min",
            interpolation="previous",
        ),
    ) is None
    snapshot = CURRENT_QUERY_SNAPSHOTS.get()["q_vxbase"]
    assert snapshot["query_scope"]["time_series_query"]["mode"] == "sample"


def test_vxbase_sampling_requires_declared_interpolation_method():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" interval("1m", "10s")'
    )

    retry = _validate_vxbase(
        inner_sql,
        _constraints(
            mode="sample",
            window="1m",
            interpolation="linear",
        ),
    )

    assert retry is not None
    assert retry.diagnostics["violated_constraint"] == "time_series_query_interpolation"


def test_vxbase_mean_aggregation_requires_avg_in_inner_sql():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" interval("1m", "10s")'
    )

    retry = _validate_vxbase(
        inner_sql,
        _constraints(
            mode="aggregate",
            window="1m",
            aggregations=[{"field": "value", "method": "mean"}],
        ),
    )

    assert retry is not None
    assert retry.diagnostics["violated_constraint"] == "time_series_native_aggregation"
    assert retry.diagnostics["missing_aggregations"] == [
        {"field": "value", "method": "avg"}
    ]


def test_vxbase_mean_aggregation_accepts_native_avg_and_interval():
    CURRENT_QUERY_SNAPSHOTS.set({})
    inner_sql = (
        f'SELECT name, ts, status, avg(value) FROM {IDENTIFIER} '
        f'WHERE ts >= "{START}" AND ts < "{END}" interval("1m", "10s")'
    )

    assert _validate_vxbase(
        inner_sql,
        _constraints(
            mode="aggregate",
            window="1 minute",
            aggregations=[{"field": "value", "method": "avg"}],
        ),
    ) is None


def test_timescale_mean_aggregation_requires_native_bucket_and_avg():
    CURRENT_QUERY_SNAPSHOTS.set({})
    constraints = _constraints(
        mode="aggregate",
        window="1m",
        aggregations=[{"field": "value", "method": "avg"}],
    )
    inner_sql = (
        "SELECT time_bucket('1 minute', \"timeStamp\") AS bucket, AVG(value) AS avg_value "
        f"FROM {IDENTIFIER} WHERE \"timeStamp\" >= '{START}' AND \"timeStamp\" < '{END}' "
        "AND status = 0 GROUP BY bucket ORDER BY bucket"
    )
    escaped = inner_sql.replace("'", "''")

    retry = validate_query_constraints(
        query_key="q_timescale",
        query_constraints=constraints,
        sql=f"SELECT * FROM ts_query('timescale', '{escaped}')",
        sql_context={
            "fragments": [
                {
                    "kind": "time_series_sql",
                    "ts_query_source": "timescale",
                    "inner_sql": inner_sql,
                    "datasource_identifier": IDENTIFIER,
                    "time_field": "timeStamp",
                    "quality_field": "status",
                    "value_fields": ["value"],
                }
            ]
        },
    )

    assert retry is None


def test_timescale_sampling_accepts_native_bucket_and_point_selection():
    CURRENT_QUERY_SNAPSHOTS.set({})
    constraints = _constraints(mode="sample", window="1 minute")
    inner_sql = (
        "SELECT time_bucket('1 minute', \"timeStamp\") AS bucket, "
        "last(value, \"timeStamp\") AS value "
        f"FROM {IDENTIFIER} WHERE \"timeStamp\" >= '{START}' AND \"timeStamp\" < '{END}' "
        "AND status = 0 GROUP BY bucket ORDER BY bucket"
    )
    escaped = inner_sql.replace("'", "''")

    retry = validate_query_constraints(
        query_key="q_timescale_sample",
        query_constraints=constraints,
        sql=f"SELECT * FROM ts_query('timescale', '{escaped}')",
        sql_context={
            "fragments": [
                {
                    "kind": "time_series_sql",
                    "ts_query_source": "timescale",
                    "inner_sql": inner_sql,
                    "datasource_identifier": IDENTIFIER,
                    "time_field": "timeStamp",
                    "quality_field": "status",
                    "value_fields": ["value"],
                }
            ]
        },
    )

    assert retry is None


def test_raw_query_over_one_day_requires_native_reduction():
    CURRENT_QUERY_SNAPSHOTS.set({})
    start = "2026-08-17T00:00:00+08:00"
    end = "2026-08-19T00:00:01+08:00"
    inner_sql = (
        f'SELECT * FROM {IDENTIFIER} '
        f'WHERE ts >= "{start}" AND ts < "{end}" limit 10000'
    )

    retry = _validate_vxbase(
        inner_sql,
        _constraints(mode="raw", start=start, end=end),
    )

    assert retry is not None
    assert retry.diagnostics["violated_constraint"] == "time_series_raw_range"


def test_new_generated_timeseries_code_requires_semantic_query_contract():
    code = f'''
execution_intent = "analysis"
query_constraints = {{
    "time_range": {{"start": "{START}", "end": "{END}"}},
    "aggregation": {{"grain": "minute", "metric": "value"}},
}}
inner_sql = 'SELECT * FROM {IDENTIFIER} WHERE ts >= "{START}" AND ts < "{END}"'
sql = "SELECT * FROM ts_query('vxbase', '" + inner_sql + "')"
validation = validate_query_constraints(query_constraints=query_constraints, sql=sql)
if validation is not None:
    result = validation
else:
    result = save_analysis_result(analysis_report="已完成时序分析", charts=[], tables=[])
'''

    retry = _validate_generated_code_contract(code)

    assert retry is not None
    assert retry.diagnostics["violated_contract"] == "missing_time_series_query_contract"


def test_existing_vxbase_metadata_is_normalized_to_physical_ts_field():
    metadata = build_table_runtime_metadata({
        "table_engine": "time_series",
        "ts_query_source": "vxbase",
        "time_field": "timeStamp",
        "value_fields": ["value"],
        "quality_field": "status",
    })

    assert metadata["time_field"] == "ts"


def test_timescale_metadata_keeps_physical_timestamp_field():
    metadata = build_table_runtime_metadata({
        "table_engine": "time_series",
        "ts_query_source": "timescale",
        "time_field": "ts",
        "value_fields": ["value"],
        "quality_field": "status",
    })

    assert metadata["time_field"] == "timeStamp"


def test_vxbase_prompt_query_contract_uses_physical_fields():
    item = _build_datasource_prompt_item({
        "datasource_id": 46,
        "datasource_type": "table",
        "datasource_name": "sensor",
        "datasource_identifier": IDENTIFIER,
        "table_engine": "time_series",
        "ts_query_source": "vxbase",
        "time_field": "timeStamp",
        "quality_field": "status",
        "value_fields": ["value"],
        "metadata_schema": {
            "properties": {
                "timeStamp": {"type": "string"},
                "status": {"type": "integer"},
                "value": {"type": "number"},
            }
        },
    })

    assert item["time_field"] == "ts"
    assert item["query_contract"]["physical_time_field"] == "ts"
    assert item["query_contract"]["allowed_fields"] == ["ts", "name", "status", "value"]


def test_vxbase_aggregate_result_column_is_normalized_to_source_field():
    import pandas as pd

    data = pd.DataFrame({"ts": [START], "avg(value)": [12.5]})

    normalized = _normalize_time_series_aggregate_result_columns(data)

    assert list(normalized.columns) == ["ts", "value"]
    assert normalized.iloc[0]["value"] == 12.5


def test_multiple_aggregations_of_same_field_get_stable_distinct_names():
    import pandas as pd

    data = pd.DataFrame({"avg(value)": [12.5], "max(value)": [18.0]})

    normalized = _normalize_time_series_aggregate_result_columns(data)

    assert list(normalized.columns) == ["value_avg", "value_max"]


def test_preparation_vxbase_schema_uses_physical_ts_field():
    snapshot = {
        "selected_datasource_snapshot": [{
            "datasource_id": 46,
            "table_engine": "time_series",
            "ts_query_source": "vxbase",
            "time_field": "timeStamp",
            "metadata_schema": {
                "properties": {
                    "timeStamp": {"type": "string", "description": "logical timestamp"},
                    "status": {"type": "integer"},
                    "value": {"type": "number"},
                }
            },
        }]
    }

    normalized = _normalize_time_series_datasource_snapshot(snapshot)
    datasource = normalized["selected_datasource_snapshot"][0]

    assert datasource["time_field"] == "ts"
    assert "ts" in datasource["metadata_schema"]["properties"]
    assert "timeStamp" not in datasource["metadata_schema"]["properties"]
    assert "timeStamp" in snapshot["selected_datasource_snapshot"][0]["metadata_schema"]["properties"]
