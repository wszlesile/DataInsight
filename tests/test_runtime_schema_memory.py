import copy

from agent.runtime_schema_memory import merge_schema_sources


def test_split_reads_preserve_field_definitions_relationships_and_do_not_mutate_inputs():
    first = {'datasource_id': 12, 'metadata_schema': {
        'properties': {'amount': {'type': 'number', 'description': 'Business amount'}},
        '$defs': {'currency': {'enum': ['USD', 'CNY']}},
        'relationships': [{'target': 13, 'source_field': 'customer_id'}],
    }, 'unavailable_field_names': ['currency', 'missing']}
    second = {'datasource_id': 12, 'metadata_schema': {
        'properties': {'currency': {'$ref': '#/$defs/currency'}},
        'relationships': [{'target': 13, 'source_field': 'customer_id'}],
    }}
    originals = copy.deepcopy([first, second])
    merged = merge_schema_sources([first], [second])
    assert set(merged[0]['metadata_schema']['properties']) == {'amount', 'currency'}
    assert merged[0]['metadata_schema']['$defs'] == first['metadata_schema']['$defs']
    assert merged[0]['metadata_schema']['relationships'] == first['metadata_schema']['relationships']
    assert merged[0]['unavailable_field_names'] == ['missing']
    assert [first, second] == originals


def test_latest_field_definition_replaces_only_that_field_without_erasing_other_sources():
    old = [{'id': 1, 'metadata_schema': {'properties': {'a': {'type': 'string'}, 'b': {}}}},
           {'id': 2, 'metadata_schema': {'properties': {'c': {}}}}]
    new = [{'datasource_id': 1, 'metadata_schema': {'properties': {'a': {'type': 'number'}}}}]
    result = merge_schema_sources(old, new)
    assert len(result) == 2
    assert result[0]['metadata_schema']['properties'] == {'a': {'type': 'number'}, 'b': {}}
    assert result[1] == old[1]
