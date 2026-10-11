"""Runtime-owned execution evidence. No model inference or semantic certification."""
from copy import deepcopy
import math

from analysis_runtime.provenance import artifact_consumption, chart_digest, snapshot_digest
from analysis_runtime.semantic_evidence import _source_details


def material_digest(traces, queries, tables, charts, report):
    # Pandas missing cells can be NaN in worker memory. Pydantic persists them
    # as JSON null. Hash the persistence representation, while frame receipts
    # continue to retain the original cell types and calculation values.
    def persisted(value):
        if isinstance(value, float) and math.isnan(value):
            return None
        if isinstance(value, dict):
            return {key: persisted(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [persisted(item) for item in value]
        return value
    return snapshot_digest(persisted({'traces': traces, 'queries': queries, 'tables': tables,
                                     'charts': charts, 'report': report}))


def build_runtime_evidence(traces, queries, tables, charts, report, coverage):
    if not traces:
        return {}
    report_consumed = any(result.get('report_spans') for result in coverage.get('results') or [])
    if not (tables or charts or report_consumed):
        return {}
    graph, consumptions, unbound = [], [], []
    facts = {}
    for trace in traces:
        token = trace['execution_token']
        sources = trace.get('sources') or {}
        recorded = trace.get('facts') or {}

        # A source-level chart can consume a successful query frame directly,
        # without an intermediate calculation. Record that actual query
        # boundary as a trace node instead of forcing the presentation through
        # a semantic formula or leaving a real chart unbound.
        for source_id, source in sources.items():
            if source.get('binding') not in {'query_frame', 'query_view', 'query_no_data'} or not (
                    source.get('query_receipt') or source.get('query_view_receipt') or source.get('no_data_receipt')):
                continue
            lineage = _source_details(source_id, sources, queries)
            node = {
                'node_id': f'{token}:query:{source_id}',
                'execution_token': token,
                'fact_id': source_id,
                'operation': 'QueryNoData' if source.get('binding') == 'query_no_data' else 'QuerySource',
                'definition': {
                    'source_id': source_id,
                    'query_keys': list(source.get('query_keys') or []),
                    'unit': '',
                },
                'input_bindings': {},
                'value_field': None,
                'input_fact_ids': [],
                'query_refs': lineage.get('query_ids') or [],
                'planned_results': [],
                'result_digest': (source.get('query_receipt') or source.get('query_view_receipt') or {}).get(
                    'frame_digest'),
                'field_origins': deepcopy(lineage.get('field_origins') or {}),
                'observations': deepcopy(lineage.get('observations') or {}),
                'reason': lineage.get('reason'),
                'execution_verification': 'query_bound' if not lineage.get('reason') else 'unresolved',
                'formula_semantics_verification': 'not_evaluated',
                'business_semantics_verification': 'not_evaluated',
            }
            graph.append(node)
            facts[(token, source_id)] = (node, {'columns': list(lineage.get('field_origins') or {})})

        def inputs(fact_id, visiting=()):
            if fact_id in visiting or fact_id not in recorded:
                return [], 'missing_or_cyclic_computation'
            fact = recorded[fact_id]
            definition = fact['definition']
            identifiers = list(fact['input_bindings'].values()) if fact.get('input_bindings') else (
                definition.get('input_source_ids') or [definition.get('source_id')])
            query_ids = []
            for identifier in identifiers:
                if identifier in recorded:
                    upstream, reason = inputs(identifier, (*visiting, fact_id))
                else:
                    detail = _source_details(identifier, sources, queries)
                    upstream, reason = detail.get('query_ids', []), detail.get('reason')
                if reason:
                    return [], reason
                query_ids.extend(upstream)
            return list(dict.fromkeys(query_ids)), None

        for fact_id, fact in recorded.items():
            query_ids, reason = inputs(fact_id)
            node = {'node_id': f'{token}:{fact_id}', 'execution_token': token, 'fact_id': fact_id,
                    'operation': fact['operation'], 'definition': deepcopy(fact['definition']),
                    'input_bindings': deepcopy(fact.get('input_bindings') or {}),
                    'value_field': fact.get('value_field'),
                    'input_fact_ids': fact.get('input_fact_ids') or [], 'query_refs': query_ids,
                    'planned_results': [deepcopy(binding) for binding in trace.get('result_bindings') or []
                                        if binding['fact_id'] == fact_id],
                    'result_digest': fact['frame_digest'], 'reason': reason,
                    'execution_verification': 'input_bound' if not reason else 'unresolved',
                    'formula_semantics_verification': 'not_evaluated',
                    'business_semantics_verification': 'not_evaluated'}
            graph.append(node)
            facts[(token, fact_id)] = (node, fact)
    for kind, artifacts in [('table', tables), ('chart', charts)]:
        for index, artifact in enumerate(artifacts):
            receipt = artifact_consumption(artifact, kind)
            pair = facts.get(
                ((receipt or {}).get('execution_token'), (receipt or {}).get('fact_id')))
            if not receipt or not pair:
                unbound.append({'artifact_type': kind, 'artifact_index': index, 'reason': 'no_runtime_consumption'})
                continue
            node, fact = pair
            fields = (receipt.get('field_mapping') if kind == 'table' else receipt.get('field_bindings')) or {}
            if kind == 'table':
                fields = {column['key']: fields[column['key']] for column in artifact.get('columns') or []
                          if column.get('key') in fields}
            if not fields and receipt.get('field'):
                fields = {receipt.get('encoded_field') or receipt['field']: receipt['field']}
            bindings = (receipt.get('cell_bindings') if kind == 'table' else None) or [
                {'field': displayed, 'source_field': source} for displayed, source in fields.items()]
            for binding in bindings:
                displayed, source = binding['field'], binding['source_field']
                available_fields = set(fact.get('columns') or [])
                if not available_fields and fact.get('values'):
                    available_fields = set(fact['values'][0])
                if (node['reason'] or available_fields and source not in available_fields
                        or not available_fields and fact.get('values') and source not in fact['values'][0]):
                    unbound.append({'artifact_type': kind, 'artifact_index': index, 'field': displayed,
                                    'reason': node['reason'] or 'result_field_missing'})
                    continue
                consumptions.append({'artifact_type': kind, 'artifact_index': index, 'field': displayed,
                    'source_field': source, 'node_id': node['node_id'],
                    **({'row_selector': deepcopy(binding['row_selector'])} if binding.get('row_selector') else {}),
                    'artifact_digest': chart_digest(artifact) if kind == 'chart' else
                        snapshot_digest({'columns': artifact['columns'], 'rows': artifact['rows']}),
                    'presentation_operation': {'view': deepcopy(receipt.get('view_operation') or {}),
                        **({'source_row': binding['source_row']} if 'source_row' in binding else {}),
                        'helper': receipt.get('helper'), 'options': deepcopy(receipt.get('helper_options') or receipt.get('options') or {}),
                        'normalization': receipt.get('normalization')}})
    report_bound = False
    if coverage.get('report_digest') == snapshot_digest(report):
        for result in coverage.get('results') or []:
            pair = facts.get((result.get('execution_token'), result.get('fact_id')))
            if not pair or pair[0]['reason']:
                continue
            for span in result.get('report_spans') or []:
                if span.get('kind') != 'display_value':
                    continue
                start, end = span['start'], span['end']
                consumptions.append({'artifact_type': 'report', 'artifact_index': 0,
                    'field': span['field'], 'source_field': span['field'], 'node_id': pair[0]['node_id'],
                    'start': start, 'end': end, 'text': report[start:end],
                    'presentation_operation': deepcopy(span)})
                report_bound = True
    if report and not report_bound:
        unbound.append({'artifact_type': 'report', 'artifact_index': 0, 'reason': 'no_runtime_report_reference'})
    return {'version': 1, 'producer': 'analysis_runtime', 'graph': graph, 'consumptions': consumptions,
            'unbound': unbound, 'business_status': 'not_evaluated',
            'material_digest': material_digest(traces, queries, tables, charts, report)}


def finalize_runtime_evidence(manifest, *, traces, queries, tables, charts, report, bindings=()):
    """Keep captured artifact edges; add only exact host-rendered finding spans."""
    if not manifest:
        return {}
    result = deepcopy(manifest)
    result['consumptions'] = [item for item in result['consumptions'] if item['artifact_type'] != 'report']
    result['unbound'] = [item for item in result['unbound'] if item['artifact_type'] != 'report']
    nodes = {(node['execution_token'], node['fact_id']): node for node in result['graph']}
    for binding in bindings:
        node = nodes.get((binding['execution_token'], binding['fact_id']))
        start, end = binding.get('start'), binding.get('end')
        if (node and not node['reason'] and isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(report) and report[start:end] == binding.get('text')):
            result['consumptions'].append({'artifact_type': 'report', 'artifact_index': 0,
                'field': ((binding['finding_id'] + '.' + binding['finding_component'])
                          if binding.get('finding_component') else binding.get('finding_id') or binding['field']),
                'source_field': binding.get('field') or binding['fact_id'], 'node_id': node['node_id'],
                'start': start, 'end': end, 'text': binding['text'],
                'presentation_operation': deepcopy(binding.get('presentation_operation')) or
                    {'kind': 'executed_finding', 'finding_id': binding['finding_id'],
                     'operation': binding.get('finding_kind')}})
    # Free prose is deliberately outside runtime evidence; no lexical/semantic scan.
    if report and not any(item['artifact_type'] == 'report' for item in result['consumptions']):
        result['unbound'].append({'artifact_type': 'report', 'artifact_index': 0, 'reason': 'no_runtime_report_reference'})
    result['material_digest'] = material_digest(traces, queries, tables, charts, report)
    return result


def compile_runtime_evidence(manifest, *, traces, queries, tables, charts, report):
    """Project saved execution receipts into the existing Evidence read model."""
    if manifest.get('version') != 1 or manifest.get('producer') != 'analysis_runtime':
        raise ValueError('Unsupported Runtime Evidence manifest')
    if manifest['material_digest'] != material_digest(traces, queries, tables, charts, report):
        raise ValueError('Runtime Evidence material changed after capture')
    nodes = {node['node_id']: node for node in manifest['graph']}
    definitions, refs = {}, {'table': [], 'chart': [], 'report': []}
    for consumption in manifest['consumptions']:
        node = nodes[consumption['node_id']]
        field = consumption['source_field']
        presentation = consumption.get('presentation_operation') or {}
        component = (presentation.get('component')
                     if presentation.get('kind') == 'display_finding_component' else None)
        identity = [node['node_id'], field]
        if component:
            identity.extend([presentation['finding_id'], component])
        key = 'runtime_' + snapshot_digest(identity)[:24]
        query_source = node.get('operation') == 'QuerySource'
        definitions.setdefault(key, {'evidence_key': key, 'metric_name': field,
            'unit': (presentation.get('unit', '') if component else
                     node['definition'].get('unit', '') if field == node.get('value_field') else ''),
            'query_refs': node['query_refs'], 'derivation': {'mode': 'runtime_execution',
                'description': ('本次执行直接查询并展示的来源字段，查询快照及展示操作可追溯。'
                                if query_source else '本次执行记录中的计算结果，已登记的输入及展示操作可追溯。')},
            'calculation': {'metric_fields': [field], 'formula': '',
                'operation_node_ids': [node['node_id']], 'executable_definition': node['definition'],
                'lineage_verification': 'query_bound' if query_source else 'execution_recorded',
                'formula_semantics_verification': 'not_evaluated', 'business_semantics_verification': 'not_evaluated'},
            'quality_notes': ['执行来源可追溯不代表公式或业务语义已验证。'],
            'transformations': ([deepcopy(consumption['presentation_operation'])]
                                if (query_source or component) and consumption.get('presentation_operation') else [])})
        kind = consumption['artifact_type']
        ref = {f'{kind}_index': consumption['artifact_index'], 'evidence_key': key,
               'presentation_binding': consumption['presentation_operation']}
        if kind == 'table':
            selector = consumption.get('row_selector')
            ref.update(column_key=consumption['field'], row_scope='selector' if selector else 'all')
            if selector:
                ref['row_selector'] = deepcopy(selector)
        elif kind == 'chart':
            ref.update(value_field=consumption['field'], point_scope='all')
        else:
            ref.update(start=consumption['start'], end=consumption['end'], text=consumption['text'])
        refs[kind].append(ref)
    return {'status': 'partial' if manifest['unbound'] else 'complete',
            'evidence_definitions': list(definitions.values()), 'table_trace_refs': refs['table'],
            'chart_trace_refs': refs['chart'], 'report_trace_refs': refs['report'], 'query_snapshots': queries,
            'diagnostics': {'producer': 'analysis_runtime', 'model_calls': 0,
                            'coverage_scope': 'explicit_runtime_consumptions',
                            'unbound': manifest['unbound'], 'business_status': 'not_evaluated'}}
