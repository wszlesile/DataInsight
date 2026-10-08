"""Result-bound presentation operations; no semantic certification of producers."""
from copy import deepcopy
from decimal import Decimal
import math
import re
from numbers import Real

import pandas as pd


def _cell(value, precision=2):
    if value is None or value is pd.NA:
        return '—'
    if isinstance(value, (Real, Decimal)) and not isinstance(value, bool):
        if pd.isna(value):
            return '—'
        text = format(value, f',.{precision}f')
        return text.rstrip('0').rstrip('.') if precision else text
    # Source labels are displayed as data, not interpreted as Markdown.
    return str(value).replace('|', r'\|').replace('\n', ' ').replace('\r', ' ')


def build_finding(trace, *, finding_id, fact_id, kind, field=None, category_field=None,
                  descending=True):
    """Resolve values only from an executed fact, never from caller-provided rows.

    Ranking/range are mathematical presentation operations, not new business
    measures. Missing values remain explicit; ties retain all labels.
    """
    if not isinstance(finding_id, str) or not finding_id:
        raise ValueError('Finding requires a nonempty identity')
    fact = trace['facts'][fact_id]
    rows = deepcopy(fact['values'])
    definition = fact.get('definition') or {}
    columns = set(rows[0]) if rows else set()
    field = field or fact.get('value_field')
    unit = definition.get('unit', '') if field == fact.get('value_field') else ''
    scope = {'input_rows': fact.get('input_rows'),
             'output_grain': definition.get('output_grain', definition.get('group_by', ())),
             'query_keys': fact.get('query_keys', ())}
    record = {'finding_id': finding_id, 'fact_id': fact_id, 'kind': kind,
              'field': field, 'category_field': category_field,
              'scope': scope, 'unit': unit, 'business_status': 'not_evaluated',
              'producer_digest': fact.get('frame_digest')}
    if kind == 'scope':
        source_ids = definition.get('input_source_ids') or [definition.get('source_id')]
        scopes = {key: {name: deepcopy(trace['sources'][key].get(name))
                        for name in ('row_count', 'grain', 'query_keys', 'binding')}
                  for key in source_ids if key in trace['sources']}
        # Input_rows may count intermediate fact rows or multiple sources. Do
        # not relabel it as an entity population or a complete month count.
        text = '；'.join(f'{_cell(key)}：进入计算的来源记录 {_cell(value["row_count"])} 行'
                        for key, value in scopes.items())
        record.update(source_scopes=scopes, text=(text or '输入包含派生计算结果。') +
                      ' 所选来源的行数不证明业务总体完整性。')
        return record
    if kind == 'table':
        fields = list(rows[0]) if rows else []
        text = ('| ' + ' | '.join(_cell(name) for name in fields) + ' |\n' +
                '|' + '|'.join('---' for _ in fields) + '|\n' +
                '\n'.join('| ' + ' | '.join(_cell(row.get(name)) for name in fields) + ' |' for row in rows)
                if rows else '该计算没有结果记录。')
        record.update(text=text, population_count=len(rows))
        return record
    if kind not in {'value', 'ranking', 'range'}:
        raise ValueError('Supported finding kinds: value, ranking, range, scope, table')
    if rows and (field not in columns or category_field and category_field not in columns):
        raise ValueError('Finding fields must exist in the executed result')
    if kind == 'value':
        if len(rows) != 1:
            raise ValueError('Value finding requires a one-row calculated result')
        record.update(value=rows[0][field], text=f'{_cell(field)}：{_cell(rows[0][field])} {unit}'.strip())
        return record
    if not category_field:
        raise ValueError('Ranking/range requires a category field')
    valid, missing = [], []
    for row in rows:
        value = row[field]
        if value is None or value is pd.NA or isinstance(value, (Real, Decimal)) and pd.isna(value):
            missing.append(row)
        elif isinstance(value, bool) or not isinstance(value, (Real, Decimal)) or not math.isfinite(value):
            raise ValueError('Ranking/range requires finite numeric values or missing values')
        else:
            valid.append(row)
    record.update(valid_count=len(valid), missing_count=len(missing), population_count=len(rows))
    if not valid:
        record['text'] = f'{_cell(field)}没有可比较的有效值。'
        return record
    ordered = sorted(valid, key=lambda row: row[field], reverse=descending)
    if kind == 'ranking':
        # Competition ranks keep tied items at the same rank. No top-N sample.
        ranked, previous, rank = [], None, 0
        for position, row in enumerate(ordered, 1):
            if previous is None or row[field] != previous:
                rank = position
            ranked.append({'rank': rank, 'label': row[category_field], 'value': row[field]})
            previous = row[field]
        text = f'| 排名 | {_cell(category_field)} | {_cell(field)}{("（" + unit + "）") if unit else ""} |\n|---|---|---|\n'
        text += '\n'.join(f'| {row["rank"]} | {_cell(row["label"])} | {_cell(row["value"])} |' for row in ranked)
        record.update(rows=ranked, descending=descending, text=text)
    else:
        high, low = max(row[field] for row in valid), min(row[field] for row in valid)
        highs = [row[category_field] for row in valid if row[field] == high]
        lows = [row[category_field] for row in valid if row[field] == low]
        record.update(maximum=high, minimum=low, highest_labels=highs, lowest_labels=lows,
                      difference=high - low)
        label = _cell(field)
        if len(valid) == 1:
            text = f'{label}仅有一个有效对象：{_cell(highs[0])}（{_cell(high)} {unit}）。'
        elif high == low:
            text = f'{label}的 {len(valid)} 个有效对象全部相等（{_cell(high)} {unit}）。'
        else:
            difference_unit = '个百分点' if unit == '%' else unit
            text = (f'{label}最高为 {"、".join(_cell(x) for x in highs)}（{_cell(high)} {unit}），'
                    f'最低为 {"、".join(_cell(x) for x in lows)}（{_cell(low)} {unit}），差值 {_cell(high-low)} {difference_unit}。')
        record['text'] = text
    if missing:
        record['text'] += f'\n\n{len(missing)} 个对象缺失该值，未参与比较。'
    return record


def compose_bound_report(traces, blocks):
    """Compose saved cells/findings and narrative; never assess semantics."""
    parts = []
    bindings = []
    offset = 0
    for block_index, block in enumerate(blocks):
        block = block.model_dump() if hasattr(block, 'model_dump') else block
        if block['kind'] == 'text':
            text = block['text']
        elif block['kind'] == 'value':
            matches = [trace for trace in traces if trace.get('execution_id') == block['trace_execution_id']
                       and block['fact_id'] in trace.get('facts', {})]
            if len(matches) != 1:
                raise ValueError('Value reference must resolve uniquely in the current execution candidate')
            trace = matches[0]
            rows = trace['facts'][block['fact_id']]['values']
            index, precision = block['row_index'], block.get('precision', 2)
            if (type(index) is not int or not 0 <= index < len(rows)
                    or type(precision) is not int or not 0 <= precision <= 12
                    or block['field'] not in rows[index]):
                raise ValueError('Value reference requires an existing row/field and precision from 0 to 12')
            text = _cell(rows[index][block['field']], precision)
            bindings.append({'execution_token': trace['execution_token'], 'fact_id': block['fact_id'],
                             'field': block['field'], 'block_index': block_index,
                             'start': offset, 'end': offset + len(text), 'text': text,
                             'presentation_operation': {'kind': 'display_value', 'row_index': index,
                                                        'precision': precision},
                             'business_status': 'not_evaluated'})
        elif block['kind'] == 'finding':
            matches = [(trace, trace.get('findings', {}).get(block['finding_id']))
                       for trace in traces if trace.get('execution_id') == block['trace_execution_id']]
            matches = [(trace, finding) for trace, finding in matches if finding is not None]
            if len(matches) != 1:
                raise ValueError('Finding reference must resolve uniquely in the current execution candidate')
            trace, finding = matches[0]
            text = finding['text']
            bindings.append({'execution_token': trace['execution_token'],
                             'finding_id': finding['finding_id'], 'fact_id': finding['fact_id'],
                             'field': finding.get('field'), 'finding_kind': finding['kind'],
                             'block_index': block_index,
                             'start': offset, 'end': offset + len(text), 'text': text,
                             'business_status': 'not_evaluated'})
        else:
            raise ValueError('Unknown report block kind')
        parts.append(text)
        offset += len(text)
    return ''.join(parts), bindings


def compose_referenced_report(traces, text, references):
    """Substitute explicit inline references, preserving authored Markdown."""
    parts, bindings, offset, cursor = [], [], 0, 0
    for match in re.finditer(r'\{\{([A-Za-z][A-Za-z0-9_]*)\}\}', text):
        prefix = text[cursor:match.start()]
        parts.append(prefix)
        offset += len(prefix)
        identity = match.group(1)
        if identity not in references:
            raise ValueError(f'Unknown report reference {identity!r}')
        rendered, resolved = compose_bound_report(traces, [references[identity]])
        parts.append(rendered)
        bindings.extend({**binding, 'reference_id': identity,
                         'start': offset + binding['start'], 'end': offset + binding['end']}
                        for binding in resolved)
        offset += len(rendered)
        cursor = match.end()
    parts.append(text[cursor:])
    return ''.join(parts), bindings
