"""Executed definition catalog for post-analysis Evidence, without prose inference."""
from copy import deepcopy
import json

from analysis_runtime.provenance import snapshot_digest, chart_digest


def _source_details(source_id, sources, query_snapshots, visiting=()):
    if source_id in visiting:
        return {'reason': 'cyclic_source_dependency'}
    source = sources.get(source_id) or {}
    if source.get('binding') == 'executed_transform':
        if source.get('transform_kind') not in {'OneToOneJoin', 'DecimalColumns'}:
            return {'reason': 'unsupported_source_transformation'}
        parents = {parent: _source_details(parent, sources, query_snapshots, (*visiting, source_id))
                   for parent in source.get('parents') or []}
        if not parents or any(parent.get('reason') for parent in parents.values()):
            return {'reason': 'unverified_transformation_input'}
        origins = {}
        for field, refs in (source.get('field_origins') or {}).items():
            resolved = []
            for ref in refs:
                parent = parents.get(ref.get('source_id')) or {}
                upstream = (parent.get('field_origins') or {}).get(ref.get('field'))
                if not upstream:
                    return {'reason': 'transformation_field_origin_missing'}
                resolved.extend(upstream)
            origins[field] = list({(item['query_key'], item['field']): item for item in resolved}.values())
        return {'reason': None, 'query_ids': list(dict.fromkeys(key for parent in parents.values() for key in parent['query_ids'])),
                'field_origins': origins, 'transformations': [
                    *{snapshot_digest(item): item for parent in parents.values() for item in parent['transformations']}.values(),
                    {'kind': source['transform_kind'], 'definition': deepcopy(source['transform']),
                     'observations': deepcopy(source.get('observations') or {})}]}
    if source.get('binding') != 'query_frame':
        return {'reason': 'transformed_or_declared_source_has_no_query_receipt'}
    receipt = source.get('query_receipt') or {}
    query_key = receipt.get('query_key')
    snapshot = query_snapshots.get(query_key) or {}
    execution = snapshot.get('execution') or {}
    if not snapshot or execution.get('status') != 'success':
        return {'reason': 'successful_query_snapshot_missing'}
    if receipt.get('snapshot_digest') != snapshot_digest(snapshot):
        return {'reason': 'query_snapshot_changed_since_input_capture'}
    if source.get('row_count') != execution.get('row_count'):
        return {'reason': 'input_population_does_not_match_query_snapshot'}
    return {'reason': None, 'query_ids': [query_key], 'transformations': [],
            'field_origins': {field: [{'query_key': query_key, 'field': field}] for field in execution.get('columns') or []}}


def build_semantic_execution_catalog(traces, query_snapshots):
    nodes = []
    for trace_index, trace in enumerate(traces):
        if not isinstance(trace, dict) or trace.get('version') != 1:
            continue
        sources = trace.get('sources') or {}
        facts = trace.get('facts') or {}
        statuses = {}
        prefix = f"semantic_{trace_index}_{trace.get('execution_token', '')}"
        for fact_id, fact in facts.items():
            definition = fact.get('definition') or {}
            source = sources.get(definition.get('source_id')) or {}
            lineage = _source_details(definition.get('source_id'), sources, query_snapshots)
            reason = lineage.get('reason')
            operation = fact.get('operation')
            input_facts = fact.get('input_fact_ids') or []
            if operation == 'PythonCalculation':
                reason = 'custom_execution_does_not_prove_input_field_usage_or_formula_correctness'
            if operation == 'DistinctCount':
                fields = [definition.get('entity_field'), *(definition.get('group_by') or [])]
            elif operation == 'RowProductSum':
                fields = [definition.get('value_field'), definition.get('quantity_field'), *(definition.get('group_by') or [])]
            else:
                fields = []
                reason = reason or 'unsupported_executed_operation'
            if not reason and not set(fields) <= set(lineage.get('field_origins') or {}):
                reason = 'input_fields_not_in_actual_query'
            if input_facts and operation != 'PythonCalculation':
                reason = 'calculated_input_composition_not_yet_compiled'
            verification = ('execution_only' if operation == 'PythonCalculation' or input_facts
                            else 'unresolved' if reason else 'query_bound')
            statuses[fact_id] = verification
            nodes.append({'node_id': f'{prefix}_fact_{fact_id}', 'kind': 'executed_fact',
                          'fact_id': fact_id, 'execution_token': trace.get('execution_token'),
                          'input_fact_node_ids': [f'{prefix}_fact_{identifier}' for identifier in input_facts],
                          'operation': operation, 'definition': deepcopy(definition),
                          'verification': verification, 'reason': reason,
                          'query_node_ids': lineage.get('query_ids', []) if not reason else [],
                          'declared_query_keys': list(fact.get('query_keys') or source.get('query_keys') or []),
                          'source_fields': fields, 'state': fact.get('state'),
                          'field_origins': {field: deepcopy(lineage['field_origins'][field]) for field in fields} if not reason else {},
                          'source_transformations': deepcopy(lineage.get('transformations', [])),
                          'value_field': fact.get('value_field'),
                          'values': deepcopy(fact.get('values') or []),
                          'unit': definition.get('unit') or '', 'unit_verification': 'declared',
                          'business_semantics_verification': 'not_evaluated'})
        for decision_id, decision in (trace.get('decisions') or {}).items():
            fact_id = decision.get('fact_id')
            nodes.append({'node_id': f'{prefix}_decision_{decision_id}', 'kind': 'executed_decision',
                          'decision_id': decision_id, 'fact_node_id': f'{prefix}_fact_{fact_id}',
                          'verification': statuses.get(fact_id, 'unresolved'),
                          'state': decision.get('state'), 'extrema_kind': decision.get('kind'),
                          'mode': decision.get('mode'), 'basis': 'mathematical',
                          'predicate': 'unique_between_comparable_items_extremum'})
    return nodes


def attach_semantic_table_bindings(outputs, tables, nodes):
    facts = {(node.get('execution_token'), node.get('fact_id')): node
             for node in nodes if node.get('kind') == 'executed_fact'}
    for output in outputs:
        if output.get('artifact_type') != 'table':
            continue
        index = output.get('artifact_index')
        if not isinstance(index, int) or not 0 <= index < len(tables):
            continue
        table = tables[index]
        binding = table.get('_semantic_execution_binding') or {}
        fact = facts.get((binding.get('execution_token'), binding.get('fact_id')))
        if not fact or output.get('field') != fact.get('value_field'):
            continue
        digest = snapshot_digest({'columns': table.get('columns') or [], 'rows': table.get('rows') or []})
        if digest != binding.get('artifact_digest'):
            output['semantic_verification'] = 'unresolved'
            output['semantic_unresolved_reason'] = 'table_changed_after_fact_view_consumption'
            continue
        output['semantic_fact_node_id'] = fact['node_id']
        output['semantic_verification'] = fact['verification']
        output['semantic_source_query_node_ids'] = fact['query_node_ids']
        fact.setdefault('output_node_ids', []).append(output['output_node_id'])
    return outputs


def attach_semantic_presentation_bindings(outputs, charts, report, coverage, nodes):
    facts = {(node.get('execution_token'), node.get('fact_id')): node
             for node in nodes if node.get('kind') == 'executed_fact'}
    spans = []
    for result in coverage.get('results') or []:
        for span in result.get('report_spans') or []:
            if span.get('kind') == 'display_value':
                spans.append(span)
            elif span.get('kind') == 'mathematical_extrema':
                spans.extend({**span, **value, 'kind': 'extrema_value'} for value in span.get('value_spans') or [])
    report_unchanged = coverage.get('report_digest') == snapshot_digest(report)
    for output in outputs:
        binding = None
        if output.get('artifact_type') == 'chart':
            index = output.get('artifact_index')
            if not isinstance(index, int) or not 0 <= index < len(charts):
                continue
            chart = charts[index]
            binding = chart.get('_semantic_execution_binding') or {}
            if binding and chart_digest(chart) != binding.get('artifact_digest'):
                output.update(semantic_verification='unresolved',
                              semantic_unresolved_reason='chart_changed_after_fact_consumption')
                continue
            if output.get('field') != (binding.get('encoded_field') or binding.get('field')):
                continue
        elif output.get('artifact_type') == 'report':
            matches = [span for span in spans if span.get('start') == output.get('start')
                       and span.get('end') == output.get('end')]
            identities = {(span.get('execution_token'), span.get('fact_id')) for span in matches}
            if len(identities) != 1:
                continue
            if not report_unchanged:
                output.update(semantic_verification='unresolved',
                              semantic_unresolved_reason='report_changed_after_fragment_consumption')
                continue
            binding = matches[0]
        else:
            continue
        fact = facts.get((binding.get('execution_token'), binding.get('fact_id')))
        if not fact:
            continue
        output.update(semantic_fact_node_id=fact['node_id'], semantic_verification=fact['verification'],
                      semantic_source_query_node_ids=fact['query_node_ids'],
                      semantic_presentation_binding=deepcopy(binding))
        if output.get('artifact_type') == 'chart' and binding.get('metric_mapping') != 'direct_rows':
            output.update(semantic_verification='execution_only',
                          semantic_unresolved_reason='display_transform_not_proven_as_fact_row')
        if fact['verification'] == 'query_bound' and output.get('artifact_type') == 'report':
            for key in ('presentation_temporal_literal', 'presentation_comparison_threshold', 'presentation_ordinal',
                        'presentation_formula_coefficient', 'presentation_business_identifier', 'presentation_display_limit',
                        'posthoc_report_candidate'):
                output[key] = False
            output['lineage_fields'] = [fact['value_field']]
        fact.setdefault('output_node_ids', []).append(output['output_node_id'])
    return outputs


def semantic_fact_operations(outputs, nodes):
    operations = []
    for fact in nodes:
        if fact.get('kind') != 'executed_fact' or fact.get('verification') != 'query_bound':
            continue
        linked = [output for output in outputs if output.get('semantic_fact_node_id') == fact['node_id']
                  and output.get('semantic_verification') == 'query_bound']
        if not linked:
            continue
        operation_id = 'op_' + fact['node_id']
        source_fields = list(dict.fromkeys(ref['field'] for refs in fact['field_origins'].values() for ref in refs))
        operations.append({'operation_node_id': operation_id, 'line': 0,
            'kind': 'native_calculation', 'semantic_kind': 'aggregation', 'targets': [], 'loads': [],
            'source': json.dumps({'definition': fact['definition'], 'transforms': fact['source_transformations']}, ensure_ascii=False, sort_keys=True, default=str),
            'query_node_ids': fact['query_node_ids'],
            'native_fact': deepcopy(fact)})
        for output in linked:
            output.update(boundary_operation_node_ids=[operation_id], direct_operation_node_ids=[operation_id],
                candidate_operation_node_ids=[operation_id], candidate_query_node_ids=fact['query_node_ids'],
                lineage_fields=source_fields, ambiguous_calculation_boundary=False,
                display_unit_hint=fact['unit'])
    return operations


def native_fact_presentation(fact):
    definition = fact['definition']
    grouping = list(definition.get('group_by') or [])
    prefix = '按' + '、'.join(grouping) + '分组，' if grouping else '在本次输入范围内，'
    if fact['operation'] == 'DistinctCount':
        formula = f"去重计数({definition['entity_field']})"
        description = prefix + f"按{definition['entity_field']}去重计数。"
    else:
        formula = f"Σ({definition['value_field']} × {definition['quantity_field']})"
        description = prefix + f"逐条计算{definition['value_field']}乘以同条{definition['quantity_field']}，然后求和。"
    transformations = []
    notes = ['来源关联和执行算法已验证；字段业务含义、数量口径及单位仍需业务确认。']
    for transform in fact.get('source_transformations') or []:
        if transform['kind'] == 'OneToOneJoin':
            observation = transform['observations']
            keys = '、'.join(transform['definition']['keys'])
            mode = transform['definition']['how']
            selection = {'inner': '仅保留两侧匹配记录', 'left': '保留全部左侧记录', 'outer': '保留两侧全部记录'}[mode]
            transformations.append(f"以{keys}校验非空唯一键后进行一对一连接，{selection}。")
            notes.append(f"连接匹配 {observation['both']} 条，左侧未匹配 {observation['left_only']} 条，右侧未匹配 {observation['right_only']} 条；{selection}。")
        else:
            transformations.append('将' + '、'.join(transform['definition']['fields']) + '显式转换为十进制数，保留缺失状态。')
    if fact['operation'] == 'RowProductSum':
        notes.append('缺失处理：' + ('明确排除缺失输入。' if definition.get('missing_policy') == 'exclude' else '缺失传播，不将缺失认定为零。'))
    return {'formula': formula, 'description': description, 'transformations': transformations,
            'quality_notes': notes, 'group_by': grouping}
