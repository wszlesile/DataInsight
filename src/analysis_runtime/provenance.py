"""Opt-in worker-local query receipts. Never infer provenance from frame attrs."""
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import pickle
import weakref
import ast
import inspect
from functools import wraps
from types import CodeType

import pandas as pd

_capture = ContextVar('semantic_execution_capture', default=None)
_program_source = ContextVar('semantic_program_source', default='')
_delivery_requirements = ContextVar('semantic_delivery_requirements', default=())
_runtime_evidence_owner = ContextVar('runtime_evidence_owner', default=False)
_frame_digesting = ContextVar('semantic_execution_frame_digesting', default=False)


def snapshot_digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


def frame_digest(frame):
    # Encode actual cell types as well as values; do not coerce Decimal/int to float.
    token = _frame_digesting.set(True)
    try:
        material = (tuple(frame.columns), tuple(map(str, frame.dtypes)), tuple(frame.index),
                    tuple(frame.itertuples(index=False, name=None)))
        return hashlib.sha256(pickle.dumps(material, protocol=5)).hexdigest()
    finally:
        _frame_digesting.reset(token)


def parameter_digest(parameters):
    # JSON default=str conflates e.g. Decimal('2') and the string '2'.
    return hashlib.sha256(pickle.dumps(parameters, protocol=5)).hexdigest()


def reset_capture():
    _capture.set(None)
    _program_source.set('')
    _delivery_requirements.set(())
    _runtime_evidence_owner.set(False)


def enable_runtime_evidence():
    """The native program port owns this path; traces alone do not select it."""
    _runtime_evidence_owner.set(True)
    _install_numeric_conversion_capture()


def _install_numeric_conversion_capture():
    """Observe the actual pandas conversion, without changing its result."""
    if getattr(pd.to_numeric, '_runtime_numeric_capture', False):
        return
    original = pd.to_numeric

    @wraps(original)
    def to_numeric(arg, *args, **kwargs):
        receipt = _series_view_receipt(arg) if _capture.get() is not None and isinstance(arg, pd.Series) else None
        output = original(arg, *args, **kwargs)
        if receipt and receipt['frame'] is not None and isinstance(output, pd.Series) and output.index.equals(arg.index):
            output = ResultViewSeries(output)
            _record_series_view(output, {'frame': weakref.ref(receipt['frame']),
                'field': receipt['field'], 'source_field': receipt['source_field']},
                {'kind': 'to_numeric', 'input': receipt['operation'],
                 'args': deepcopy(args), 'options': deepcopy(kwargs)})
        return output

    to_numeric._runtime_numeric_capture = True
    pd.to_numeric = to_numeric


def runtime_evidence_enabled():
    return _runtime_evidence_owner.get()


def set_delivery_requirements(requirements):
    """Host-owned identities, not declarations supplied by the generated save call."""
    _delivery_requirements.set(tuple(dict(item) for item in requirements))


def calculation_delivery_requirements(plan):
    required = []
    for question in plan.get('questions') or []:
        for rule in question.get('calculation_rules') or []:
            for result_id in rule.get('output_fact_ids') or []:
                item = {'question_id': question['question_id'], 'result_id': result_id}
                if item not in required:
                    required.append(item)
    return required


def execute_registered_definitions():
    """The host schedules actual definitions, never infers algorithms from a plan."""
    for execution in (_capture.get() or {}).get('executions', []):
        execution.run()


def execution_delivery_coverage(tables=(), charts=(), report=''):
    """Read current producers; captured execution alone does not certify presentation."""
    executions = (_capture.get() or {}).get('executions', [])
    rows = []
    required = list(_delivery_requirements.get())
    identities = {(item['question_id'], item['result_id']) for item in required}
    observed = [dict(item) for item in required]
    for execution in executions:
        for identity in execution._result_bindings:
            if not any((item['question_id'], item['result_id']) == identity for item in observed):
                observed.append({'question_id': identity[0], 'result_id': identity[1]})
    for requirement in observed:
        identity = (requirement['question_id'], requirement['result_id'])
        producers = [(execution, execution._result_bindings[identity]) for execution in executions
                     if identity in execution._result_bindings]
        row = {**requirement, 'required_by_plan': identity in identities,
               'status': 'missing' if not producers else
               'ambiguous' if len(producers) != 1 else 'executed',
               'presentation_status': 'not_verified', 'business_status': 'not_evaluated'}
        if len(producers) == 1:
            execution, fact_id = producers[0]
            fact = execution._facts[fact_id]
            row.update(execution_token=execution._token, fact_id=fact_id,
                       operation=type(fact['definition']).__name__, fact_state=fact['state'])
            consumed = []
            for index, table in enumerate(tables):
                binding = table.get('_semantic_execution_binding') or {}
                digest = snapshot_digest({'columns': table.get('columns'), 'rows': table.get('rows')})
                key = (execution._token, fact_id, digest)
                if (binding.get('execution_token') == execution._token and binding.get('fact_id') == fact_id
                        and binding.get('artifact_digest') == digest
                        and key in (_capture.get() or {}).get('presentations', set())):
                    consumed.append(index)
            row['table_indices'] = consumed
            chart_indices = []
            for index, chart in enumerate(charts):
                binding = chart.get('_semantic_execution_binding') or {}
                digest = chart_digest(chart)
                if (binding.get('execution_token') == execution._token and binding.get('fact_id') == fact_id
                        and binding.get('artifact_digest') == digest
                        and (binding.get('field') == fact['value_field'] or fact['value_field'] in
                             (binding.get('field_bindings') or {}).values())
                        and (execution._token, fact_id, digest) in (_capture.get() or {}).get('charts', set())):
                    chart_indices.append(index)
            row['chart_indices'] = chart_indices
            saved_report = (_capture.get() or {}).get('reports', {}).get(id(report))
            report_spans = saved_report[1] if saved_report and saved_report[0] is report else []
            row['report_spans'] = [deepcopy(item) for item in report_spans
                                   if item['execution_token'] == execution._token and item['fact_id'] == fact_id]
            channels = (['table'] if consumed else []) + (['chart'] if chart_indices else []) + (['report'] if row['report_spans'] else [])
            row['consumed_channels'] = channels
            row['presentation_status'] = '_and_'.join(channels) + '_consumed' if channels else 'not_verified'
        rows.append(row)
    return {'version': 1, 'mode': 'required' if rows else 'legacy', 'results': rows,
            'report_digest': snapshot_digest(str(report)) if report else None,
            'execution_status': 'incomplete' if any(row['required_by_plan'] and row['status'] != 'executed' for row in rows)
                                else 'covered' if required else 'not_required',
            'presentation_status': 'not_verified', 'business_status': 'not_evaluated'}


def set_program_source(code):
    _program_source.set(code)


def callable_definition(function):
    code = getattr(function, '__code__', None)
    source = ''
    if code is not None:
        program = _program_source.get()
        if program:
            for node in ast.walk(ast.parse(program)):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                        and node.lineno == code.co_firstlineno):
                    source = ast.get_source_segment(program, node) or ''
                    break
        if not source:
            try:
                source = inspect.getsource(function)
            except (OSError, TypeError):
                pass
    return {'name': getattr(function, '__qualname__', type(function).__qualname__),
            'module': getattr(function, '__module__', type(function).__module__),
            'source': source, 'source_status': 'captured' if source else 'unavailable',
            'code_sha256': snapshot_digest(_code_definition(code)) if code else '',
            'closure_names': list(code.co_freevars) if code else []}


def _code_definition(code):
    def constant(value):
        if isinstance(value, CodeType):
            return _code_definition(value)
        if isinstance(value, tuple):
            return {'tuple': [constant(item) for item in value]}
        if isinstance(value, frozenset):
            return {'frozenset': sorted((constant(item) for item in value), key=repr)}
        return {'type': type(value).__name__, 'value': repr(value)}
    # marshal's interned-string flags can change after execution. Hash stable
    # code semantics instead, including nested functions and closed variable names.
    return {'bytecode': code.co_code.hex(), 'constants': [constant(value) for value in code.co_consts],
            'names': code.co_names, 'variables': code.co_varnames, 'freevars': code.co_freevars,
            'cellvars': code.co_cellvars, 'flags': code.co_flags,
            'arguments': (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount)}


def register_execution(execution):
    capture = _capture.get()
    if capture is None:
        capture = {'queries': {}, 'query_views': {}, 'series_views': {}, 'executions': [], 'views': {}, 'presentations': {},
                   'charts': {}, 'fragments': {}, 'reports': {}}
        _capture.set(capture)
    capture['executions'].append(execution)


def record_query_frame(query_key, frame, snapshot):
    capture = _capture.get()
    if capture is None or not isinstance(frame, pd.DataFrame):
        return
    if (snapshot.get('execution') or {}).get('status') != 'success':
        return
    try:
        receipt = {'frame': weakref.ref(frame), 'query_key': query_key,
                   'frame_digest': frame_digest(frame), 'snapshot': deepcopy(snapshot),
                   'snapshot_digest': snapshot_digest(snapshot)}
    except (TypeError, ValueError, pickle.PickleError):
        # Unsupported cell serialization cannot break an existing query path.
        return
    capture['queries'][id(frame)] = receipt
    record_query_view(frame, receipt)


def record_query_view(frame, receipt, *, field_mapping=None, operation=None):
    """Track a query-backed frame through supported, field-preserving views."""
    capture = _capture.get()
    if capture is None or not isinstance(frame, pd.DataFrame):
        return
    prior = query_view_receipt(frame)
    if prior:
        base = prior
    else:
        snapshot = receipt.get('snapshot') or {}
        base = {
            'query_key': receipt.get('query_key'),
            'snapshot_digest': receipt.get('snapshot_digest'),
            'query_frame_digest': receipt.get('frame_digest'),
            'query_row_count': int(len(frame)),
            'field_mapping': {str(field): str(field) for field in frame.columns},
            'view_operation': {'kind': 'query_result'},
        }
    mapped = dict(field_mapping if field_mapping is not None else base.get('field_mapping') or {})
    base.update({
        'frame_digest': frame_digest(frame),
        'field_mapping': mapped,
        'view_operation': deepcopy(operation or base.get('view_operation') or {'kind': 'query_result'}),
    })
    capture['query_views'][id(frame)] = {'frame': weakref.ref(frame), **base}


def query_view_receipt(frame):
    capture = _capture.get()
    receipt = (capture or {}).get('query_views', {}).get(id(frame))
    if receipt is None or receipt['frame']() is not frame:
        return None
    if receipt['frame_digest'] != frame_digest(frame):
        return None
    return {key: deepcopy(value) for key, value in receipt.items() if key != 'frame'}


def _frame_view_receipt(frame):
    return fact_view_receipt(frame) or query_view_receipt(frame)


def _record_frame_view(frame, receipt, *, field_mapping, operation):
    if 'execution_token' in receipt:
        record_fact_view(frame, receipt['execution_token'], receipt['fact_id'],
                         field_mapping=field_mapping, operation=operation)
        return
    record_query_view(frame, receipt, field_mapping=field_mapping, operation=operation)


def _series_digest(series):
    token = _frame_digesting.set(True)
    try:
        material = (str(series.dtype), tuple(series.index), tuple(series.tolist()))
        return hashlib.sha256(pickle.dumps(material, protocol=5)).hexdigest()
    finally:
        _frame_digesting.reset(token)


def _record_series_view(series, receipt, operation):
    capture = _capture.get()
    if capture is None or not isinstance(series, pd.Series):
        return
    capture.setdefault('series_views', {})[id(series)] = {
        'series': weakref.ref(series), 'frame': receipt['frame'],
        'field': receipt['field'], 'source_field': receipt['source_field'],
        'series_digest': _series_digest(series), 'operation': deepcopy(operation),
    }


def _series_view_receipt(series):
    receipt = (_capture.get() or {}).get('series_views', {}).get(id(series))
    if (receipt is None or receipt['series']() is not series
            or receipt['series_digest'] != _series_digest(series)):
        return None
    return {key: value for key, value in receipt.items() if key not in {'series', 'frame'}} | {
        'frame': receipt['frame']()}


def query_receipt(frame):
    capture = _capture.get()
    receipt = (capture or {}).get('queries', {}).get(id(frame))
    if receipt is None or receipt['frame']() is not frame:
        raise ValueError('Input is not a query frame captured in this worker execution')
    if frame_digest(frame) != receipt['frame_digest']:
        raise ValueError('Query frame was modified; transformed input needs its own lineage')
    return deepcopy({key: value for key, value in receipt.items() if key != 'frame'})


def execution_traces():
    return [execution.trace() for execution in (_capture.get() or {}).get('executions', [])
            if execution._facts or any(source.get('query_receipt') or source.get('query_view_receipt')
                                       for source in execution._sources.values())]


def record_fact_view(frame, execution_token, fact_id, *, field_mapping=None, operation=None):
    capture = _capture.get()
    if capture is not None:
        capture['views'][id(frame)] = {'frame': weakref.ref(frame), 'frame_digest': frame_digest(frame),
                                     'execution_token': execution_token, 'fact_id': fact_id,
                                     'field_mapping': dict(field_mapping or {field: field for field in frame.columns}),
                                     'view_operation': deepcopy(operation or {'kind': 'saved_result'})}


def fact_view_receipt(frame):
    receipt = (_capture.get() or {}).get('views', {}).get(id(frame))
    if receipt is None or receipt['frame']() is not frame or receipt['frame_digest'] != frame_digest(frame):
        return None
    return {key: value for key, value in receipt.items() if key != 'frame'}


def _dtype_description(dtype):
    """Keep cast declarations serializable across persisted execution receipts."""
    if isinstance(dtype, dict):
        return {str(field): _dtype_description(value) for field, value in dtype.items()}
    return dtype if isinstance(dtype, str) else str(dtype)


class ResultViewFrame(pd.DataFrame):
    """Detached result view with receipts for known display-only operations.

    No attrs-based recovery or value matching. A mutated input cannot pass its
    receipt to another frame, and arbitrary pandas transformations stay unbound.
    """
    def copy(self, deep=True):
        receipt = _frame_view_receipt(self)
        output = super().copy(deep=deep)
        output = ResultViewFrame(output)
        if receipt:
            _record_frame_view(output, receipt, field_mapping=receipt['field_mapping'],
                               operation=receipt['view_operation'])
        return output

    @property
    def _constructor_sliced(self):
        return ResultViewSeries

    def __getitem__(self, key):
        result = super().__getitem__(key)
        if _frame_digesting.get():
            return result
        receipt = _frame_view_receipt(self)
        if (receipt and isinstance(result, pd.DataFrame)
                and isinstance(key, (list, pd.Index))
                and all(isinstance(field, str) for field in key)
                and result.columns.is_unique):
            result = ResultViewFrame(result)
            _record_frame_view(result, receipt,
                field_mapping={field: source for field, source in receipt['field_mapping'].items()
                               if field in result.columns},
                operation={'kind': 'column_projection', 'input': receipt['view_operation'],
                           'columns': list(key), 'output_rows': len(result)})
        elif (receipt and isinstance(key, str) and isinstance(result, pd.Series)
              and key in receipt.get('field_mapping', {})):
            _record_series_view(result, {'frame': weakref.ref(self), 'field': key,
                'source_field': receipt['field_mapping'][key]}, {'kind': 'source_field', 'field': key})
        return result

    @property
    def iloc(self):
        return _ReceiptIndexer(self, super().iloc, 'iloc')

    def __setitem__(self, key, value):
        receipt = _frame_view_receipt(self)
        series_receipt = _series_view_receipt(value) if isinstance(value, pd.Series) else None
        super().__setitem__(key, value)
        if receipt:
            assigned = set(key) if isinstance(key, (list, tuple, pd.Index)) else {key}
            mapping = {field: source for field, source in receipt['field_mapping'].items()
                       if field in self.columns and field not in assigned}
            if (series_receipt and series_receipt['frame'] is self
                    and isinstance(key, str)
                    and value.index.equals(self.index)):
                mapping[key] = series_receipt['source_field']
            _record_frame_view(self, receipt, field_mapping=mapping, operation={
                'kind': 'column_assignment',
                'input': receipt['view_operation'],
                'assigned_fields': [str(field) for field in assigned],
                'preserved_source_fields': list(mapping),
                'source_series_operation': deepcopy(series_receipt['operation']) if series_receipt
                    and series_receipt['frame'] is self and isinstance(key, str)
                    and value.index.equals(self.index) else None})

    def insert(self, loc, column, value, allow_duplicates=False):
        receipt = _frame_view_receipt(self)
        super().insert(loc, column, value, allow_duplicates=allow_duplicates)
        if receipt:
            mapping = {field: source for field, source in receipt['field_mapping'].items()
                       if field in self.columns and field != column}
            _record_frame_view(self, receipt, field_mapping=mapping, operation={
                'kind': 'column_insertion', 'input': receipt['view_operation'],
                'inserted_field': str(column), 'preserved_source_fields': list(mapping)})

    def reset_index(self, *args, **kwargs):
        receipt = _frame_view_receipt(self)
        output = super().reset_index(*args, **kwargs)
        if receipt and isinstance(output, pd.DataFrame):
            output = ResultViewFrame(output)
            mapping = {field: source for field, source in receipt['field_mapping'].items()
                       if field in output.columns}
            _record_frame_view(output, receipt, field_mapping=mapping, operation={
                'kind': 'reset_index', 'input': receipt['view_operation'],
                'drop': bool(kwargs.get('drop', False)), 'output_rows': len(output)})
        return output

    def rename(self, *args, **kwargs):
        receipt = _frame_view_receipt(self)
        output = super().rename(*args, **kwargs)
        if receipt and isinstance(output, pd.DataFrame):
            output = ResultViewFrame(output)
            columns = kwargs.get('columns')
            axis = kwargs.get('axis')
            if columns is None and args and axis in (1, 'columns'):
                columns = args[0]
            mapping = {}
            if isinstance(columns, dict):
                for field, source in receipt['field_mapping'].items():
                    renamed = columns.get(field, field)
                    if renamed in output.columns:
                        mapping[renamed] = source
            elif columns is None:
                mapping = {field: source for field, source in receipt['field_mapping'].items()
                           if field in output.columns}
            _record_frame_view(output, receipt, field_mapping=mapping, operation={
                'kind': 'rename', 'input': receipt['view_operation'],
                'columns': deepcopy(columns) if isinstance(columns, dict) else {},
                'output_rows': len(output)})
        return output

    def astype(self, dtype, copy=True, errors='raise'):
        receipt = _frame_view_receipt(self)
        output = super().astype(dtype=dtype, copy=copy, errors=errors)
        if receipt and isinstance(output, pd.DataFrame):
            output = ResultViewFrame(output)
            mapping = {field: source for field, source in receipt['field_mapping'].items()
                       if field in output.columns}
            _record_frame_view(output, receipt, field_mapping=mapping, operation={
                'kind': 'astype', 'input': receipt['view_operation'],
                'dtype': _dtype_description(dtype),
                'errors': errors, 'output_rows': len(output)})
        return output

    def round(self, decimals=0, *args, **kwargs):
        receipt = _frame_view_receipt(self)
        output = super().round(decimals=decimals, *args, **kwargs)
        if receipt and isinstance(output, pd.DataFrame):
            output = ResultViewFrame(output)
            mapping = {field: source for field, source in receipt['field_mapping'].items()
                       if field in output.columns}
            _record_frame_view(output, receipt, field_mapping=mapping, operation={
                'kind': 'round', 'input': receipt['view_operation'],
                'decimals': deepcopy(decimals), 'output_rows': len(output)})
        return output

    def sort_values(self, *args, **kwargs):
        receipt = _frame_view_receipt(self)
        output = super().sort_values(*args, **kwargs)
        if output is not None:
            output = ResultViewFrame(output)
        target = self if output is None else output
        if receipt:
            _record_frame_view(target, receipt, field_mapping=receipt['field_mapping'], operation={
                'kind': 'sort', 'input': receipt['view_operation'],
                'by': deepcopy(args[0] if args else kwargs.get('by')),
                'options': deepcopy({key: value for key, value in kwargs.items() if key != 'by'})})
        return output


class _ReceiptIndexer:
    """Carry source-field receipts through deterministic DataFrame iloc views."""
    def __init__(self, frame, indexer, name):
        self._frame = frame
        self._indexer = indexer
        self._name = name

    def __getattr__(self, name):
        return getattr(self._indexer, name)

    def __setitem__(self, key, value):
        self._indexer[key] = value

    def __getitem__(self, key):
        if _frame_digesting.get():
            return self._indexer[key]
        receipt = _frame_view_receipt(self._frame)
        output = self._indexer[key]
        if not receipt or not isinstance(output, pd.DataFrame):
            return output
        output = ResultViewFrame(output)
        mapping = {field: source for field, source in receipt['field_mapping'].items()
                   if field in output.columns}
        row_selector = key[0] if isinstance(key, tuple) and key else key
        selector = ({'type': 'slice', 'start': row_selector.start,
                     'stop': row_selector.stop, 'step': row_selector.step}
                    if isinstance(row_selector, slice) else
                    {'type': type(row_selector).__name__})
        _record_frame_view(output, receipt, field_mapping=mapping, operation={
            'kind': self._name, 'input': receipt['view_operation'],
            'row_selector': selector, 'output_rows': len(output)})
        return output


class ResultViewSeries(pd.Series):
    """Series view that carries only explicit field-preserving cast/round operations."""
    @property
    def _constructor(self):
        return ResultViewSeries

    def astype(self, dtype, copy=True, errors='raise'):
        receipt = _series_view_receipt(self)
        output = super().astype(dtype=dtype, copy=copy, errors=errors)
        if receipt:
            operation = {'kind': 'astype', 'input': receipt['operation'],
                         'dtype': _dtype_description(dtype),
                         'errors': errors}
            _record_series_view(output, {'frame': weakref.ref(receipt['frame']),
                'field': receipt['field'], 'source_field': receipt['source_field']}, operation)
        return output

    def round(self, decimals=0, *args, **kwargs):
        receipt = _series_view_receipt(self)
        output = super().round(decimals=decimals, *args, **kwargs)
        if receipt:
            operation = {'kind': 'round', 'input': receipt['operation'],
                         'decimals': deepcopy(decimals)}
            _record_series_view(output, {'frame': weakref.ref(receipt['frame']),
                'field': receipt['field'], 'source_field': receipt['source_field']}, operation)
        return output


def record_table_consumption(receipt, digest):
    capture = _capture.get()
    if capture is not None:
        capture['presentations'][(receipt['execution_token'], receipt['fact_id'], digest)] = deepcopy(receipt)


def refresh_table_consumption(table):
    """Rebind an already-recorded table receipt after JSON persistence normalization."""
    if not isinstance(table, dict):
        return table
    binding = table.get('_semantic_execution_binding') or {}
    old_digest = binding.get('artifact_digest')
    old_key = (binding.get('execution_token'), binding.get('fact_id'), old_digest)
    capture = _capture.get() or {}
    receipt = capture.get('presentations', {}).get(old_key)
    if not receipt or not old_digest:
        return table
    digest = snapshot_digest({'columns': table.get('columns') or [], 'rows': table.get('rows') or []})
    updated = deepcopy(receipt)
    record_table_consumption(updated, digest)
    table['_semantic_execution_binding'] = {**binding, 'artifact_digest': digest}
    return table


def artifact_consumption(artifact, kind):
    binding = artifact.get('_semantic_execution_binding') or {}
    digest = (chart_digest(artifact) if kind == 'chart' else
              snapshot_digest({'columns': artifact.get('columns'), 'rows': artifact.get('rows')}))
    key = (binding.get('execution_token'), binding.get('fact_id'), digest)
    receipt = (_capture.get() or {}).get('charts' if kind == 'chart' else 'presentations', {}).get(key)
    if receipt and binding.get('artifact_digest') == digest:
        # Use the registry, never caller-modified metadata with a matching digest.
        return deepcopy(receipt)
    return None


def chart_digest(chart):
    return snapshot_digest({key: value for key, value in chart.items()
                            if key not in {'_semantic_execution_binding', '_artifact_id'}})


def record_chart_consumption(chart, receipt):
    digest = chart_digest(chart)
    binding = {**receipt, 'artifact_digest': digest}
    _capture.get()['charts'][(receipt['execution_token'], receipt['fact_id'], digest)] = deepcopy(binding)
    chart['_semantic_execution_binding'] = binding
    return chart


def bind_chart_frame(chart, frame, options):
    """Record the actual helper input; transformations are execution facts."""
    receipt = fact_view_receipt(frame)
    if not receipt:
        return chart
    fields = options.get('value_fields') or [options.get('value_field'), options.get('x_field'), options.get('y_field')]
    mapping = receipt['field_mapping']
    selected = {field: mapping[field] for field in fields if field in mapping}
    if selected:
        record_chart_consumption(chart, {**receipt, 'field_bindings': selected,
            'helper_options': deepcopy(options), 'verification': 'actual_helper_consumption',
            'business_status': 'not_evaluated'})
    return chart


def normalize_chart_consumption(original, normalized):
    binding = original.get('_semantic_execution_binding') or {}
    digest = chart_digest(original)
    key = (binding.get('execution_token'), binding.get('fact_id'), digest)
    receipt = (_capture.get() or {}).get('charts', {}).get(key)
    if receipt and binding.get('artifact_digest') == digest:
        original_encoding = ((original.get('chart_data_contract') or {}).get('encoding') or
                             (original.get('chart_document') or {}).get('encoding') or {})
        normalized_encoding = (normalized.get('chart_data_contract') or {}).get('encoding') or {}
        encoded_field = receipt.get('encoded_field') or receipt.get('field')
        before_fields = original_encoding.get('value_fields') or [original_encoding.get('value_field')]
        after_fields = normalized_encoding.get('value_fields') or [normalized_encoding.get('value_field')]
        if encoded_field in before_fields and len(before_fields) == len(after_fields):
            encoded_field = after_fields[before_fields.index(encoded_field)]
        else:
            for axis in ('x_field', 'y_field'):
                if original_encoding.get(axis) == encoded_field:
                    encoded_field = normalized_encoding.get(axis)
                    break
        field_bindings = dict(receipt.get('field_bindings') or {})
        for before, after in zip(before_fields, after_fields):
            if before in field_bindings and before != after:
                field_bindings[after] = field_bindings.pop(before)
        return record_chart_consumption(normalized, {**receipt, 'field_bindings': field_bindings,
            'encoded_field': encoded_field,
            'source_artifact_digest': digest, 'normalization': 'existing_chart_normalizer'})
    if binding:
        normalized['_semantic_execution_binding'] = dict(binding)
    return normalized


class ReportFragment(str):
    """Rendered text; authority lives in the worker registry, not this type."""


class ComposedReport(str):
    """Exact composed object, preventing proof by equal text."""


def record_report_fragment(text, receipt, *, start=0, end=None):
    fragment = ReportFragment(text)
    _capture.get()['fragments'][id(fragment)] = (fragment, {**receipt, 'start': start,
                                                         'end': len(text) if end is None else end})
    return fragment


def compose_report(parts):
    if isinstance(parts, (str, bytes)):
        raise ValueError('Report composition needs a sequence of text parts')
    capture = _capture.get()
    text, spans = '', []
    for part in parts:
        if not isinstance(part, str):
            raise TypeError('Report parts must be rendered fragments or strings')
        saved = (capture or {}).get('fragments', {}).get(id(part))
        if saved and saved[0] is part:
            receipt = saved[1]
            shifted = {**receipt, 'start': len(text) + receipt['start'], 'end': len(text) + receipt['end']}
            if receipt.get('value_spans'):
                shifted['value_spans'] = [{**span, 'start': len(text) + span['start'],
                                           'end': len(text) + span['end']} for span in receipt['value_spans']]
            spans.append(shifted)
        saved_report = (capture or {}).get('reports', {}).get(id(part))
        if saved_report and saved_report[0] is part:
            for receipt in saved_report[1]:
                shifted = {**receipt, 'start': len(text) + receipt['start'], 'end': len(text) + receipt['end']}
                if receipt.get('value_spans'):
                    shifted['value_spans'] = [{**span, 'start': len(text) + span['start'],
                                               'end': len(text) + span['end']} for span in receipt['value_spans']]
                spans.append(shifted)
        text += str(part)
    leading = len(text) - len(text.lstrip())
    report = ComposedReport(text.strip())
    spans = [{**span, 'start': max(0, span['start'] - leading),
              'end': min(len(report), span['end'] - leading)} for span in spans
             if span['end'] > leading and span['start'] < leading + len(report)]
    for span in spans:
        if span.get('value_spans'):
            span['value_spans'] = [{**value, 'start': value['start'] - leading,
                                    'end': value['end'] - leading} for value in span['value_spans']]
    if capture is not None:
        capture['reports'][id(report)] = (report, spans)
    return report
