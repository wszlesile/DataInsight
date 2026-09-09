"""Lossless source/field memory for exact Runtime Schema reads."""

import copy


def merge_schema_sources(existing, incoming):
    by_id = {}
    for source in [*(existing or []), *(incoming or [])]:
        if not isinstance(source, dict):
            continue
        try:
            identity = int(source.get('datasource_id') or source.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if identity <= 0:
            continue
        old = by_id.get(identity, {})
        merged = {**copy.deepcopy(old), **copy.deepcopy(source)}
        old_schema = old.get('metadata_schema') or {}
        new_schema = source.get('metadata_schema') or {}
        schema = {**copy.deepcopy(old_schema), **copy.deepcopy(new_schema)}
        schema['properties'] = {
            **copy.deepcopy(old_schema.get('properties') or {}),
            **copy.deepcopy(new_schema.get('properties') or {}),
        }
        for key in ('$defs', 'definitions'):
            if key in old_schema or key in new_schema:
                schema[key] = {
                    **copy.deepcopy(old_schema.get(key) or {}),
                    **copy.deepcopy(new_schema.get(key) or {}),
                }
        if 'relationships' in old_schema or 'relationships' in new_schema:
            relationships = []
            for item in [*(old_schema.get('relationships') or []),
                         *(new_schema.get('relationships') or [])]:
                if item not in relationships:
                    relationships.append(copy.deepcopy(item))
            schema['relationships'] = relationships
        merged['metadata_schema'] = schema
        missing = list(dict.fromkeys([
            *(old.get('unavailable_field_names') or []),
            *(source.get('unavailable_field_names') or []),
        ]))
        missing = [field for field in missing if field not in schema['properties']]
        if missing:
            merged['unavailable_field_names'] = missing
        else:
            merged.pop('unavailable_field_names', None)
        by_id[identity] = merged
    return list(by_id.values())
