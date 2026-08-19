import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.insight_ns_rel_datasource_service import (  # noqa: E402
    InsightNsRelDatasourceService,
)


def test_reference_targets_already_in_import_batch_are_preserved(monkeypatch):
    service = InsightNsRelDatasourceService(session=None)
    details = [
        {
            'id': 'relational_target',
            'alias': 'relational_alias',
            'dataType': 2,
        },
        {
            'id': 'time_series_target',
            'alias': 'time_series_alias',
            'dataType': 1,
        },
        {
            'id': 'relational_reference',
            'alias': 'relational_reference_alias',
            'dataType': 7,
            'refers': [{
                'id': 'relational_target',
                'alias': 'relational_alias',
            }],
        },
        {
            'id': 'time_series_reference',
            'alias': 'time_series_reference_alias',
            'dataType': 7,
            'refers': [{
                'id': 'time_series_target',
                'alias': 'time_series_alias',
            }],
        },
    ]

    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError('in-batch reference targets must not be fetched again')

    monkeypatch.setattr(
        service,
        '_fetch_uns_details_in_parallel',
        unexpected_fetch,
    )

    reference_details, failed = service._fetch_reference_uns_details_for_import(
        details,
        authorization='Bearer test',
    )

    assert failed == []
    assert {
        node_id: detail['dataType']
        for node_id, detail in reference_details.items()
    } == {
        'relational_target': 2,
        'time_series_target': 1,
    }
