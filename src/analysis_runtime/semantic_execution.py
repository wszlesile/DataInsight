"""Worker APIs for an opt-in single-producer execution chain.

Native definitions execute here, not via a second model-written formula.
Receipts identify this execution's snapshots. Views return copies, decisions
consume saved facts, and rendering consumes saved decisions. This is not a
business-rule/natural-language certifier or a sandbox for arbitrary Python.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from numbers import Integral
from typing import Literal, Callable, Any
from uuid import uuid4
from decimal import Decimal, InvalidOperation

import pandas as pd

from analysis_runtime.calculations import _finite_number, summarize_extrema
from analysis_runtime.expressions import _render_extrema_summary
from analysis_runtime.provenance import frame_digest, query_receipt, query_view_receipt, register_execution, record_fact_view, callable_definition, snapshot_digest
from analysis_runtime.provenance import parameter_digest
from analysis_runtime.provenance import record_chart_consumption, record_report_fragment, compose_report


@dataclass(frozen=True)
class DistinctCount:
    definition_id: str
    source_id: str
    entity_field: str
    group_by: tuple[str, ...] = ()
    value_field: str = 'value'
    unit: str = ''


@dataclass(frozen=True)
class RowProductSum:
    definition_id: str
    source_id: str
    value_field: str
    quantity_field: str
    group_by: tuple[str, ...] = ()
    output_field: str = 'value'
    unit: str = ''
    missing_policy: Literal['propagate', 'exclude'] = 'propagate'


@dataclass(frozen=True)
class FactRef:
    execution_token: str
    definition_id: str


@dataclass(frozen=True)
class DecisionRef:
    execution_token: str
    decision_id: str


@dataclass(frozen=True)
class OneToOneJoin:
    source_id: str
    left_source_id: str
    right_source_id: str
    keys: tuple[str, ...]
    how: Literal['inner', 'left', 'outer'] = 'inner'
    suffixes: tuple[str, str] = ('_x', '_y')


@dataclass(frozen=True)
class DecimalColumns:
    source_id: str
    input_source_id: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class PythonCalculation:
    """Calculation callbacks read named DataFrames through inputs.frame(source_id)."""
    definition_id: str
    function: Callable
    input_source_ids: tuple[str, ...]
    parameters: dict[str, Any] = field(default_factory=dict)
    value_field: str | None = 'value'
    unit: str = ''
    output_grain: tuple[str, ...] = ()
    value_kind: Literal['numeric', 'object'] = 'numeric'


class CalculationInputs(dict):
    """Named, detached source frames passed to a PythonCalculation callback.

    String-key mapping access remains available for stored definitions; new
    definitions should use ``frame(source_id)`` so source access is explicit.
    """

    __slots__ = ()

    def __init__(self, frames: dict[str, pd.DataFrame]):
        super().__init__(frames)

    def __getitem__(self, source_id):
        if not isinstance(source_id, str):
            raise TypeError(
                'CalculationInputs uses named source identities, not positions; '
                'read a frame with inputs.frame(source_id)'
            )
        return super().__getitem__(source_id)

    def frame(self, source_id: str) -> pd.DataFrame:
        if not isinstance(source_id, str) or not source_id:
            raise TypeError('Calculation input access requires a source or definition identity string')
        try:
            return self[source_id]
        except KeyError as exc:
            available = ', '.join(sorted(self))
            raise KeyError(f'Unknown calculation input {source_id!r}; available identities: {available}') from exc


def _copy_frame(frame):
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ValueError('Source must be a DataFrame with unique columns')
    result = frame.copy(deep=True)
    for column in result.columns:
        if result[column].dtype == object:
            result[column] = pd.Series([deepcopy(value) for value in result[column]],
                                       index=result.index, dtype=object)
    return result


def _fields(frame, names):
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise ValueError(
            f'Definition references an absent field: missing_fields={missing!r}, '
            f'available_fields={list(frame.columns)!r}, row_count={len(frame)}'
        )


class SemanticExecution:
    def __init__(self, execution_id: str):
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError('execution_id must be a non-empty string')
        self.execution_id = execution_id
        self._token = str(uuid4())
        self._sources = {}
        self._facts = {}
        self._result_bindings = {}
        self._decisions = {}
        self._findings = {}
        self._definitions = {}
        self._source_definitions = {}
        self._definition_bindings = {}
        self._decision_definitions = {}
        self._evaluating = set()
        register_execution(self)

    def define(self, definition, *, question_id=None, result_id=None):
        """Register the actual algorithm. Dependencies are evaluated on consumption."""
        if not isinstance(definition, (DistinctCount, RowProductSum, PythonCalculation)):
            raise TypeError('Expected an executable calculation definition')
        identifier = definition.definition_id
        if not identifier or identifier in self._sources or identifier in self._facts or identifier in self._source_definitions:
            raise ValueError('Definition must have a new result identity')
        if identifier in self._definitions and self._definitions[identifier] is not definition:
            raise ValueError('Result already has an executable definition')
        if question_id and result_id is None:
            result_id = identifier
        if bool(question_id) != bool(result_id):
            raise ValueError('Supply both question and result identity')
        if question_id:
            identity = (question_id, result_id)
            previous = self._definition_bindings.get(identifier)
            if previous is not None and previous != identity:
                raise ValueError('Definition already has a different planned identity')
            self._definition_bindings[identifier] = identity
        self._definitions[identifier] = definition
        return identifier

    def resolve_identity(self, identity, *, calculated_only=False):
        """Resolve execution and planned identities through one registry."""
        if isinstance(identity, tuple) and len(identity) == 2:
            matches = {key for key, binding in self._definition_bindings.items() if binding == identity}
        else:
            registered = set(self._definitions) | set(self._facts)
            if not calculated_only:
                registered |= set(self._sources) | set(self._source_definitions)
            matches = {identity} if identity in registered else set()
            matches.update(key for key, binding in self._definition_bindings.items() if binding[1] == identity)
        if len(matches) > 1:
            raise ValueError(f'Result identity {identity!r} is ambiguous; use (question_id, result_id)')
        if not matches:
            raise KeyError(identity)
        return next(iter(matches))

    def result(self, definition_id):
        """Execute dependencies and their consumer once; publish its actual binding."""
        return self._execute_result(self.resolve_identity(definition_id, calculated_only=True))

    def _execute_result(self, definition_id):
        """Schedule a canonical identity already resolved by the registry."""
        if definition_id in self._evaluating:
            raise ValueError('Cyclic executable calculation dependency')
        if definition_id in self._facts:
            reference = FactRef(self._token, definition_id)
            if definition_id in self._definition_bindings:
                self.bind_result(*self._definition_bindings[definition_id], reference)
            return reference
        definition = self._definitions[definition_id]
        inputs = (definition.input_source_ids if isinstance(definition, PythonCalculation)
                  else (definition.source_id,))
        self._evaluating.add(definition_id)
        try:
            for identifier in inputs:
                self._ensure_input(identifier)
            reference = self.execute(definition)
            if definition_id in self._definition_bindings:
                self.bind_result(*self._definition_bindings[definition_id], reference)
            return reference
        finally:
            self._evaluating.remove(definition_id)

    def define_extrema(self, decision_id, fact_id, *, mode='max', field=None):
        """Register a mathematical decision, not a second prose predicate."""
        definition = (fact_id, mode, field)
        if not decision_id or mode not in {'max', 'min'}:
            raise ValueError('Decision identity and max/min mode required')
        previous = self._decision_definitions.get(decision_id)
        if previous is not None and previous != definition:
            raise ValueError('Decision already has another executable definition')
        self._decision_definitions[decision_id] = definition
        return decision_id

    def decision(self, decision_id):
        fact_id, mode, field = self._decision_definitions[decision_id]
        identity = self.resolve_identity(fact_id)
        self._ensure_input(identity)
        return self._evaluate_extrema_input(decision_id, identity, mode=mode, field=field)

    def run(self):
        """Evaluate the registered program, sharing all intermediate results."""
        for identifier in self._source_definitions:
            self._ensure_input(identifier)
        results = {identifier: self._execute_result(identifier) for identifier in self._definitions}
        for identifier in self._decision_definitions:
            self.decision(identifier)
        return results

    def define_transform(self, definition):
        if not isinstance(definition, (OneToOneJoin, DecimalColumns)):
            raise TypeError('Expected an executable source transformation')
        identifier = definition.source_id
        if not identifier or identifier in self._sources or identifier in self._definitions or identifier in self._facts:
            raise ValueError('Transformation requires a new identity')
        previous = self._source_definitions.get(identifier)
        if previous is not None and previous != definition:
            raise ValueError('Transformation already has another definition')
        self._source_definitions[identifier] = definition
        return identifier

    def _ensure_input(self, identifier):
        identifier = self.resolve_identity(identifier)
        if identifier in self._sources or identifier in self._facts:
            return
        if identifier in self._definitions:
            self._execute_result(identifier)
            return
        definition = self._source_definitions[identifier]
        if identifier in self._evaluating:
            raise ValueError('Cyclic executable source dependency')
        parents = ((definition.left_source_id, definition.right_source_id)
                   if isinstance(definition, OneToOneJoin) else (definition.input_source_id,))
        self._evaluating.add(identifier)
        try:
            for parent in parents:
                self._ensure_input(parent)
            self.materialize(definition)
        finally:
            self._evaluating.remove(identifier)

    def _calculation_input(self, identifier):
        identifier = self.resolve_identity(identifier)
        if identifier in self._sources:
            return self._sources[identifier]
        fact = self._facts[identifier]
        definition = fact['definition']
        grain = (definition.output_grain if isinstance(definition, PythonCalculation)
                 else definition.group_by)
        return {'frame': fact['frame'], 'query_keys': fact['query_keys'], 'grain': grain}

    def add_query_source(self, source_id, frame, *, grain=()):
        """Bind an unchanged, actually returned query frame from this worker."""
        receipt = query_receipt(frame)
        self.add_source(source_id, frame, query_key=receipt['query_key'], grain=grain)
        self._sources[source_id]['query_receipt'] = receipt

    def add_query_view_source(self, source_id, frame, receipt, *, grain=()):
        """Bind a supported field-preserving view of an executed query frame."""
        query_key = receipt.get('query_key')
        if not query_key or not receipt.get('field_mapping'):
            raise ValueError('Query view receipt must identify its source query and fields')
        self.add_source(source_id, frame, query_key=query_key, grain=grain)
        self._sources[source_id]['query_view_receipt'] = deepcopy(receipt)

    def add_query_no_data_source(self, source_id, snapshot, *, grain=()):
        """Retain a physical empty-query outcome without inventing result fields."""
        execution = snapshot.get('execution') or {}
        query_key = snapshot.get('query_key')
        if (not query_key or not (snapshot.get('implementation') or {}).get('physical_execution')
                or execution.get('status') != 'no_data' or execution.get('row_count') != 0):
            raise ValueError('Empty source requires a physically executed no-data query receipt')
        self.add_source(source_id, pd.DataFrame(), query_key=query_key)
        self._sources[source_id]['no_data_receipt'] = {
            'query_key': query_key, 'snapshot_digest': snapshot_digest(snapshot)}
        self._sources[source_id]['observations'] = {
            'status': 'no_data', 'row_count': 0, 'declared_grain': list(grain),
            'error_type': execution.get('error_type'),
            'columns_available': False,
        }

    def add_source(self, source_id, frame, *, query_key=None, query_keys=(), grain=()):
        if source_id in self._source_definitions:
            raise ValueError('Source already has an executable transformation')
        self._add_source(source_id, frame, query_key=query_key, query_keys=query_keys, grain=grain)

    def _add_source(self, source_id, frame, *, query_key=None, query_keys=(), grain=()):
        if query_key is not None and query_keys:
            raise ValueError('Supply one query key or multiple query keys, not both')
        keys = (query_key,) if query_key is not None else tuple(query_keys)
        if (not source_id or not keys or any(not isinstance(key, str) or not key for key in keys)
                or len(keys) != len(set(keys)) or source_id in self._sources or source_id in self._facts
                or source_id in self._definitions):
            raise ValueError('Source identity/query key required; duplicate source identity')
        frame = _copy_frame(frame)
        if isinstance(grain, str):
            raise ValueError('Grain must be a sequence of field names')
        grain = tuple(grain)
        if len(grain) != len(set(grain)):
            raise ValueError('Duplicate source grain fields')
        _fields(frame, grain)
        if grain and (frame[list(grain)].isna().any().any() or frame.duplicated(list(grain)).any()):
            null_rows = int(frame[list(grain)].isna().any(axis=1).sum())
            duplicate_rows = int(frame.duplicated(list(grain), keep=False).sum())
            raise ValueError(
                f'Actual rows violate the declared source grain: source_id={source_id!r}, '
                f'grain={grain!r}, row_count={len(frame)}, null_key_rows={null_rows}, '
                f'duplicate_key_rows={duplicate_rows}, available_fields={list(frame.columns)!r}. '
                'Source grain describes unique physical input records, not the later aggregation groups. '
                'Use the actual record key from Schema/data; if it is not established, leave grain=() '
                'and retain that unknown rather than declaring a false unique key.'
            )
        self._sources[source_id] = {'frame': frame, 'query_keys': keys, 'grain': grain}

    def materialize(self, definition):
        """Execute a supported source transformation once; retain its actual inputs."""
        if not isinstance(definition, (OneToOneJoin, DecimalColumns)):
            raise TypeError('Unsupported source transformation')
        existing = self._sources.get(definition.source_id)
        if existing is not None:
            if existing.get('transform') != definition:
                raise ValueError('Source identity already has another producer')
            return definition.source_id
        if not definition.source_id or definition.source_id in self._facts:
            raise ValueError('Transformation cannot overwrite an existing identity')
        if isinstance(definition, OneToOneJoin):
            if (not isinstance(definition.keys, tuple) or not definition.keys
                    or len(set(definition.keys)) != len(definition.keys)
                    or definition.how not in {'inner', 'left', 'outer'}
                    or not isinstance(definition.suffixes, tuple) or len(definition.suffixes) != 2):
                raise ValueError('Explicit immutable join keys, mode and suffixes required')
            parents = [definition.left_source_id, definition.right_source_id]
            left, right = [self._calculation_input(parent)['frame'] for parent in parents]
            keys = list(definition.keys)
            for frame in (left, right):
                _fields(frame, keys)
                if frame[keys].isna().any().any() or frame.duplicated(keys).any():
                    raise ValueError('Join keys must be non-null and unique on both inputs')
            # Use a temporary indicator independent of user fields.
            marker = '__semantic_join_match__'
            while marker in left.columns or marker in right.columns:
                marker += '_'
            outer = left.astype(object).merge(right.astype(object), on=keys, how='outer', validate='one_to_one',
                               suffixes=definition.suffixes, indicator=marker)
            if not outer.columns.is_unique:
                raise ValueError('Join suffixes produce ambiguous output fields')
            counts = outer[marker].value_counts()
            observation = {label: int(counts.get(label, 0)) for label in ('both', 'left_only', 'right_only')}
            mask = (outer[marker].eq('both') if definition.how == 'inner' else
                    ~outer[marker].eq('right_only') if definition.how == 'left' else
                    pd.Series(True, index=outer.index))
            values = outer.loc[mask].drop(columns=marker).reset_index(drop=True)
            mapping = {}
            overlap = (set(left.columns) & set(right.columns)) - set(keys)
            for parent, frame, suffix in zip(parents, (left, right), definition.suffixes):
                for field in frame.columns:
                    output = str(field) + suffix if field in overlap else field
                    mapping.setdefault(output, []).append({'source_id': parent, 'field': field})
            grain = definition.keys
        else:
            if (not isinstance(definition.fields, tuple) or not definition.fields
                    or len(set(definition.fields)) != len(definition.fields)):
                raise ValueError('Explicit immutable decimal conversion fields required')
            parents = [definition.input_source_id]
            source = self._calculation_input(parents[0])
            values = _copy_frame(source['frame'])
            _fields(values, definition.fields)
            def decimal_value(value):
                if value is None or pd.isna(value):
                    return None
                if isinstance(value, bool):
                    raise ValueError('Boolean is not a decimal amount or quantity')
                try:
                    number = Decimal(str(value))
                except (ValueError, InvalidOperation) as exc:
                    raise ValueError('Invalid decimal input') from exc
                if not number.is_finite():
                    raise ValueError('Non-finite decimal input')
                return number
            for field in definition.fields:
                values[field] = pd.Series([decimal_value(value) for value in values[field]],
                                          index=values.index, dtype=object)
            mapping = {field: [{'source_id': parents[0], 'field': field}] for field in values.columns}
            observation = {'converted_fields': definition.fields}
            grain = source['grain']
        query_keys = tuple(dict.fromkeys(key for parent in parents for key in self._calculation_input(parent)['query_keys']))
        self._add_source(definition.source_id, values, query_keys=query_keys, grain=grain)
        self._sources[definition.source_id].update(transform=definition, parents=tuple(parents),
            field_origins=mapping, observations={**observation, 'output_rows': len(values)})
        return definition.source_id

    def source_table(self, source_id):
        """Read an executed source without modifying the authoritative input."""
        source = self._sources[source_id]
        from analysis_runtime.provenance import ResultViewFrame
        output = ResultViewFrame(_copy_frame(source['frame']))
        if source.get('query_receipt') or source.get('query_view_receipt'):
            receipt = source.get('query_view_receipt') or {}
            record_fact_view(output, self._token, source_id,
                             # Presentation addresses this saved source's fields.
                             # Its query view receipt separately maps those fields
                             # to upstream query columns in the evidence graph.
                             field_mapping=({field: field for field in receipt['field_mapping']}
                                            if receipt.get('field_mapping') else None),
                             operation={'kind': 'query_source',
                                        'query_keys': list(source['query_keys']),
                                        'row_count': len(output),
                                        'source_view': deepcopy(receipt.get('view_operation'))})
        return output

    def has_source(self, source_id):
        """Tell presentation callers whether an identity names a query source."""
        return source_id in self._sources

    def source_observations(self, source_id):
        return deepcopy(self._sources[source_id].get('observations') or {})

    def execute(self, definition):
        registered = self._definitions.get(getattr(definition, 'definition_id', None))
        if registered is not None and registered is not definition:
            raise ValueError('Result already has another registered executable definition')
        if isinstance(definition, PythonCalculation):
            return self._execute_python(definition)
        if not isinstance(definition, (DistinctCount, RowProductSum)):
            raise TypeError('This increment implements distinct count and row product sum only')
        if not definition.definition_id or not isinstance(definition.group_by, tuple):
            raise ValueError('Definition identity and immutable grouping required')
        if definition.definition_id in self._sources:
            raise ValueError('Calculated identity cannot overwrite a source identity')
        previous = self._facts.get(definition.definition_id)
        if previous is not None:
            if previous['definition'] != definition:
                raise ValueError('A result identity already has a different producer; use a new version/execution')
            return FactRef(self._token, definition.definition_id)
        source = self._calculation_input(definition.source_id)
        frame = source['frame']
        groups = definition.group_by
        if len(groups) != len(set(groups)):
            raise ValueError('Duplicate grouping fields')
        _fields(frame, groups)
        if isinstance(definition, DistinctCount):
            _fields(frame, (definition.entity_field,))
            output_field = definition.value_field
            if output_field in groups:
                raise ValueError('Output field cannot overwrite a grouping key')
            if frame[definition.entity_field].isna().any():
                raise ValueError('Missing entity identity cannot be counted silently')
            if groups:
                values = frame.groupby(list(groups), dropna=False, observed=True)[definition.entity_field].nunique().reset_index(name=output_field)
            else:
                values = (pd.DataFrame(columns=[output_field]) if frame.empty else
                          pd.DataFrame([{output_field: int(frame[definition.entity_field].nunique())}]))
            missing_count = 0
        else:
            if not source['grain']:
                raise ValueError('Row product sum requires an explicit, checked input grain')
            if definition.missing_policy not in {'propagate', 'exclude'}:
                raise ValueError('Unknown missing-value policy')
            _fields(frame, (definition.value_field, definition.quantity_field))
            output_field = definition.output_field
            if output_field in groups:
                raise ValueError('Output field cannot overwrite a grouping key')
            products = []
            for left, right in zip(frame[definition.value_field], frame[definition.quantity_field]):
                left, right = _finite_number(left), _finite_number(right)
                left = int(left) if isinstance(left, Integral) else left
                right = int(right) if isinstance(right, Integral) else right
                products.append(None if left is None or right is None else _finite_number(left * right))
            missing_count = sum(value is None for value in products)

            def total(indices):
                selected = [products[index] for index in indices]
                valid = [value for value in selected if value is not None]
                if not valid or (definition.missing_policy == 'propagate' and len(valid) != len(selected)):
                    return None
                return _finite_number(sum(valid))

            if groups:
                # Positional indices preserve correctness with duplicate pandas indices.
                positional = frame.reset_index(drop=True)
                rows = []
                for key, indices in positional.groupby(list(groups), dropna=False, observed=True).indices.items():
                    key = key if len(groups) > 1 else (key,)
                    rows.append({**dict(zip(groups, key)), output_field: total(indices)})
                values = pd.DataFrame(rows, columns=[*groups, output_field])
                values[output_field] = pd.Series([row[output_field] for row in rows], dtype=object)
            else:
                values = pd.DataFrame({output_field: pd.Series([] if frame.empty else [total(range(len(frame)))], dtype=object)})
        state = ('empty' if frame.empty else 'missing' if values[output_field].isna().any()
                 else 'partial' if missing_count else 'valid')
        self._facts[definition.definition_id] = {'definition': definition, 'frame': _copy_frame(values),
            'state': state, 'missing_count': missing_count, 'input_rows': len(frame),
            'input_bindings': {definition.source_id: self.resolve_identity(definition.source_id)},
            'value_field': output_field, 'query_keys': source['query_keys']}
        return FactRef(self._token, definition.definition_id)

    def _execute_python(self, definition):
        if (not definition.definition_id or not callable(definition.function)
                or not isinstance(definition.input_source_ids, tuple)
                or not isinstance(definition.parameters, dict)
                or not isinstance(definition.output_grain, tuple)
                or definition.value_kind not in {'numeric', 'object'}
                or len(set(definition.input_source_ids)) != len(definition.input_source_ids)):
            raise ValueError('Custom calculation needs one callable, immutable input identities and parameter object')
        if definition.definition_id in self._sources:
            raise ValueError('Calculated identity cannot overwrite a source identity')
        implementation = callable_definition(definition.function)
        record = {'definition_id': definition.definition_id, 'implementation': implementation,
                  'input_source_ids': definition.input_source_ids, 'parameters': deepcopy(definition.parameters),
                  'value_field': definition.value_field, 'unit': definition.unit, 'output_grain': definition.output_grain,
                  'value_kind': definition.value_kind,
                  'parameter_digest': parameter_digest(definition.parameters)}
        fingerprint = snapshot_digest(record)
        previous = self._facts.get(definition.definition_id)
        if previous is not None:
            if (previous.get('producer_fingerprint') != fingerprint
                    or previous['definition'].function is not definition.function):
                raise ValueError('A result identity already has a different producer; use a new version/execution')
            return FactRef(self._token, definition.definition_id)
        input_frames = {source_id: _copy_frame(self._calculation_input(source_id)['frame'])
                        for source_id in definition.input_source_ids}
        inputs = CalculationInputs(input_frames)
        input_rows = sum(len(frame) for frame in input_frames.values())
        output = definition.function(inputs, deepcopy(definition.parameters))
        values = (_copy_frame(output) if isinstance(output, pd.DataFrame) else
                  pd.DataFrame({definition.value_field or 'value': pd.Series([deepcopy(output)], dtype=object)}))
        if definition.value_field is None:
            if definition.value_kind != 'object' or not isinstance(output, pd.DataFrame):
                raise ValueError('A field-free custom result must return a DataFrame with value_kind="object"')
            _fields(values, definition.output_grain)
        else:
            missing_fields = [field for field in (*definition.output_grain, definition.value_field)
                              if field not in values.columns]
            if missing_fields:
                if isinstance(output, pd.DataFrame) and definition.value_field not in values.columns:
                    raise ValueError(
                        f"Calculation {definition.definition_id!r} declares value_field="
                        f"{definition.value_field!r}, but returned columns are {list(values.columns)!r}; "
                        "for a numeric measure in a multi-field DataFrame, keep value_field and value_kind='numeric'; "
                        "use value_field=None and value_kind='object' only when no primary numeric measure exists"
                    )
                _fields(values, missing_fields)
        if definition.output_grain and (values[list(definition.output_grain)].isna().any().any()
                or values.duplicated(list(definition.output_grain)).any()):
            raise ValueError('Custom result violates declared output grain')
        # Validate the selected numeric column, without changing arbitrary auxiliary output columns.
        if definition.value_kind == 'numeric' and definition.value_field is not None:
            for value in values[definition.value_field]:
                _finite_number(value)
        missing_count = (int(values[definition.value_field].isna().sum())
                         if definition.value_field is not None else int(values.isna().sum().sum()))
        state = ('empty' if values.empty else
                 'missing' if missing_count == (len(values) if definition.value_field is not None
                                                  else values.size) else
                 'partial' if missing_count else 'valid')
        definition_record = deepcopy(record)
        definition_record['value_field'] = definition.value_field
        self._facts[definition.definition_id] = {
            'definition': definition, 'definition_record': definition_record, 'producer_fingerprint': fingerprint,
            'input_bindings': {identifier: self.resolve_identity(identifier) for identifier in definition.input_source_ids},
            'frame': values, 'state': state,
            'missing_count': missing_count, 'input_rows': input_rows,
            'value_field': definition.value_field,
            'query_keys': tuple(dict.fromkeys(key for source_id in inputs
                                            for key in self._calculation_input(source_id)['query_keys'])),
        }
        return FactRef(self._token, definition.definition_id)

    def _fact(self, reference):
        if not isinstance(reference, FactRef) or reference.execution_token != self._token:
            raise ValueError('Fact receipt belongs to another execution')
        return self._facts[reference.definition_id]

    def table(self, reference):
        from analysis_runtime.provenance import ResultViewFrame
        frame = ResultViewFrame(_copy_frame(self._fact(reference)['frame']))
        record_fact_view(frame, self._token, reference.definition_id)
        return frame

    def bind_result(self, question_id, result_id, reference):
        """Bind a planned result to its actual producer, without reimplementing it."""
        self._fact(reference)
        if not question_id or not result_id:
            raise ValueError('Question and planned result identities are required')
        identity = (question_id, result_id)
        if identity in self._result_bindings and self._result_bindings[identity] != reference.definition_id:
            raise ValueError('Planned result already has a different producer')
        self._result_bindings[identity] = reference.definition_id
        return reference

    def chart(self, reference, *, helper='result', **options):
        """Pass saved facts to existing chart helpers, retaining actual consumption."""
        from utils.chart_document_utils import (build_chart_result, build_chart_document,
            build_multi_metric_chart_result, build_chart_suite, normalize_chart_result_item)
        builders = {'result': build_chart_result, 'document': build_chart_document,
                    'multi_metric': build_multi_metric_chart_result, 'suite': build_chart_suite}
        if helper not in builders or 'data' in options:
            raise ValueError('Choose an existing chart helper; data comes from the saved fact')
        fact = self._fact(reference)
        output = builders[helper](data=self.table(reference), **options)
        if helper == 'document':
            output = {'chart_document': output}
        items = output if isinstance(output, list) else [output]
        normalized = []
        for item in items:
            chart = normalize_chart_result_item(item)
            chart_kind = (item.get('chart_document') or {}).get('chart_kind')
            keys = [key for key in (options.get('category_field'), options.get('series_field')) if key]
            direct_rows = (chart_kind == 'scatter' or chart_kind in {'bar', 'line'} and bool(keys)
                           and not fact['frame'].duplicated(keys).any())
            encoding = (chart.get('chart_data_contract') or {}).get('encoding') or {}
            fields = {encoding.get('value_field'), encoding.get('y_field'), encoding.get('x_field'),
                      *(encoding.get('value_fields') or [])}
            if fact['value_field'] in fields:
                record_chart_consumption(chart, {**(chart.get('_semantic_execution_binding') or {}), 'execution_token': self._token,
                    'fact_id': reference.definition_id, 'field': fact['value_field'],
                    'helper': helper, 'options': deepcopy(options),
                    'metric_mapping': 'direct_rows' if direct_rows else 'display_transform',
                    'verification': 'actual_helper_consumption', 'business_status': 'not_evaluated'})
            normalized.append(chart)
        return normalized if isinstance(output, list) else normalized[0]

    def render_value(self, reference, *, row=0, value_format='', prefix='', suffix=''):
        fact = self._fact(reference)
        value = fact['frame'].iloc[row][fact['value_field']]
        text = format(value, value_format) if value_format else str(value)
        rendered = prefix + text + suffix
        return record_report_fragment(rendered, {'execution_token': self._token,
            'fact_id': reference.definition_id, 'field': fact['value_field'], 'row': row,
            'value_format': value_format,
            'kind': 'display_value', 'business_status': 'not_evaluated'},
            start=len(prefix), end=len(prefix) + len(text))

    @staticmethod
    def compose_report(parts):
        return compose_report(parts)

    def finding(self, finding_id, reference, *, kind, field=None, category_field=None, descending=True):
        """Produce a presentation finding from this execution's saved result."""
        fact = self._fact(reference)
        from analysis_runtime.findings import build_finding
        definition = (fact['definition_record'] if 'definition_record' in fact else asdict(fact['definition']))
        input_ids = definition.get('input_source_ids') or [definition.get('source_id')]
        # Do not serialize every result and source frame for each presentation
        # operation. Only this fact and its directly declared source scopes.
        view = {'facts': {reference.definition_id: {
            'values': fact['frame'].to_dict(orient='records'), 'definition': definition,
            'value_field': fact['value_field'], 'input_rows': fact['input_rows'],
            'query_keys': fact['query_keys'], 'frame_digest': frame_digest(fact['frame'])}},
            'sources': {key: {'row_count': len(self._sources[key]['frame']),
                'grain': self._sources[key]['grain'], 'query_keys': self._sources[key]['query_keys'],
                'binding': 'executed_input'} for key in input_ids if key in self._sources}}
        finding = build_finding(view, finding_id=finding_id,
            fact_id=reference.definition_id, kind=kind, field=field,
            category_field=category_field, descending=descending)
        previous = self._findings.get(finding_id)
        if previous is not None and previous != finding:
            raise ValueError('Finding identity already has another result/operation')
        self._findings[finding_id] = finding
        return deepcopy(finding)

    def fact_state(self, reference):
        fact = self._fact(reference)
        return {'state': fact['state'], 'missing_count': fact['missing_count'],
                'input_rows': fact['input_rows'], 'unit': fact['definition'].unit}

    def evaluate_extrema(self, decision_id, reference, *, mode='max'):
        self._fact(reference)
        return self._evaluate_extrema_input(decision_id, reference.definition_id, mode=mode)

    def _comparison_input(self, identity, field=None):
        if identity in self._facts:
            fact = self._facts[identity]
            if field is not None and field != fact['value_field']:
                raise ValueError('Calculated extrema consume the registered primary value field')
            return {**fact, 'unit': fact['definition'].unit}
        source = self._sources[identity]
        if field is None:
            raise ValueError('Query-source extrema require field=<actual numeric result column>')
        _fields(source['frame'], (field,))
        missing = sum(_finite_number(value) is None for value in source['frame'][field])
        return {'frame': source['frame'], 'value_field': field, 'unit': '',
                'missing_count': missing, 'state': 'empty' if source['frame'].empty
                else 'partial' if missing else 'valid'}

    def _evaluate_extrema_input(self, decision_id, identity, *, mode='max', field=None):
        fact = self._comparison_input(identity, field)
        if isinstance(fact.get('definition'), PythonCalculation) and fact['definition'].value_kind != 'numeric':
            raise ValueError('Object output is not a numeric comparison fact')
        if not decision_id:
            raise ValueError('Decision identity required')
        definition = (identity, mode, fact['value_field'])
        previous = self._decisions.get(decision_id)
        if previous is not None:
            if previous['definition'] != definition:
                raise ValueError('Decision identity already has another producer')
            return DecisionRef(self._token, decision_id)
        if fact['state'] == 'empty':
            comparable = fact['frame'].iloc[:0]
        else:
            comparable = fact['frame']
        summary = summarize_extrema(comparable, fact['value_field'], mode=mode)
        # The predicate is ONLY "has a unique between-item extremum".
        state = ('unknown' if summary['kind'] == 'empty' else 'not_applicable' if summary['kind'] == 'single'
                 else 'true' if summary['kind'] == 'unique' else 'false')
        self._decisions[decision_id] = {'definition': definition, 'summary': summary, 'state': state,
            'input_scope': {key: fact[key] for key in ('value_field', 'unit', 'state', 'missing_count')}}
        return DecisionRef(self._token, decision_id)

    def _decision(self, reference):
        if not isinstance(reference, DecisionRef) or reference.execution_token != self._token:
            raise ValueError('Decision receipt belongs to another execution')
        return self._decisions[reference.decision_id]

    def decision_state(self, reference):
        decision = self._decision(reference)
        return {'state': decision['state'], 'kind': decision['summary']['kind'],
                'basis': 'mathematical', 'predicate': 'unique_between_comparable_items_extremum',
                'comparison_scope': 'valid_values',
                'complete_inputs': decision['input_scope']['missing_count'] == 0,
                'valid_count': decision['summary']['valid_count'],
                'missing_count': decision['summary']['missing_count']}

    def matches(self, reference):
        from analysis_runtime.provenance import ResultViewFrame
        decision = self._decision(reference)
        identity, mode, field = decision['definition']
        output = ResultViewFrame(_copy_frame(decision['summary']['rows']))
        source = self._sources.get(identity)
        if source is None or source.get('query_receipt') or source.get('query_view_receipt'):
            mapping = ((source.get('query_view_receipt') or {}).get('field_mapping') if source else None)
            record_fact_view(output, self._token, identity, field_mapping=mapping,
                             operation={'kind': 'extrema_selection', 'mode': mode, 'field': field,
                                        'decision_id': reference.decision_id, 'output_rows': len(output)})
        return output

    def render_extrema(self, reference, label_field, *, value_label='', language='zh'):
        decision = self._decision(reference)
        fact = decision['input_scope']
        field_spans = []
        rendered = _render_extrema_summary(decision['summary'], label_field,
            value_label=value_label or fact['value_field'], unit=fact['unit'], language=language,
            field_spans=field_spans)
        if fact['state'] == 'partial':
            rendered += (f" 计算明确排除了 {fact['missing_count']} 条缺失输入记录，结果不代表完整总体。" if language == 'zh'
                         else f" The calculation explicitly excludes {fact['missing_count']} missing input records; results do not cover the full population.")
        return record_report_fragment(rendered, {'execution_token': self._token,
            'fact_id': decision['definition'][0], 'decision_id': reference.decision_id,
            'kind': 'mathematical_extrema', 'business_status': 'not_evaluated',
            'value_spans': [span for span in field_spans if span['field'] == 'value']})

    def trace(self):
        return {'execution_id': self.execution_id, 'execution_token': self._token, 'version': 1,
            'result_bindings': [{'question_id': key[0], 'result_id': key[1], 'fact_id': value}
                                for key, value in self._result_bindings.items()],
            'sources': {key: {'query_keys': value['query_keys'], 'grain': value['grain'],
                              'row_count': len(value['frame']),
                              'columns': list(value['frame'].columns),
                              'binding': 'query_no_data' if 'no_data_receipt' in value else
                                         'executed_transform' if 'transform' in value else
                                         'query_frame' if 'query_receipt' in value else
                                         'query_view' if 'query_view_receipt' in value else 'declared',
                              'query_receipt': deepcopy(value.get('query_receipt')),
                              'query_view_receipt': deepcopy(value.get('query_view_receipt')),
                              'no_data_receipt': deepcopy(value.get('no_data_receipt')),
                              'transform': asdict(value['transform']) if 'transform' in value else None,
                              'transform_kind': type(value['transform']).__name__ if 'transform' in value else None,
                              'parents': value.get('parents', ()),
                              'field_origins': deepcopy(value.get('field_origins', {})),
                              'observations': deepcopy(value.get('observations', {}))}
                        for key, value in self._sources.items()},
            'facts': {key: {'definition': deepcopy(value['definition_record']) if 'definition_record' in value else asdict(value['definition']), 'state': value['state'],
                            'operation': type(value['definition']).__name__,
                            'value_field': value['value_field'],
                            'values': deepcopy(value['frame'].to_dict(orient='records')),
                            'frame_digest': frame_digest(value['frame']),
                            'query_keys': value['query_keys'], 'input_rows': value['input_rows'],
                            'missing_count': value['missing_count'],
                            'input_bindings': deepcopy(value['input_bindings']),
                            'input_fact_ids': list(dict.fromkeys(identifier for identifier in
                                value['input_bindings'].values() if identifier in self._facts))}
                      for key, value in self._facts.items()},
            'findings': deepcopy(self._findings),
            'decisions': {key: {'fact_id': value['definition'][0], 'mode': value['definition'][1],
                                'value_field': value['definition'][2],
                                'state': value['state'], 'kind': value['summary']['kind']}
                          for key, value in self._decisions.items()}}
