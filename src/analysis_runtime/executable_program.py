"""Native executable definition module; host assembly does not rewrite algorithms."""
import ast
from copy import deepcopy
from dataclasses import replace
from typing import Callable
import pandas as pd
from analysis_runtime.provenance import query_receipt
from analysis_runtime.semantic_execution import DistinctCount, RowProductSum, PythonCalculation, OneToOneJoin, DecimalColumns

class ExecutableResults:
    """Presentation reads saved outputs; this is not a Python or semantic sandbox."""
    def __init__(self, execution):
        self._execution = execution

    def _definition_id(self, identity):
        """Resolve either a calculation id or its planned semantic result identity."""
        return self._execution.resolve_identity(identity, calculated_only=True)

    def table(self, fact_id):
        """Read the registered producer's saved frame, preserving its identity."""
        identity = self._execution.resolve_identity(fact_id)
        if self._execution.has_source(identity):
            return self._execution.source_table(identity)
        return self._execution.table(self._execution._execute_result(identity))

    def status(self, fact_id):
        """Return execution metadata only; calculated values are available through table()."""
        return self._execution.fact_state(self._execution._execute_result(self._definition_id(fact_id)))

    def view(self, fact_id, *, columns=None, rename=None):
        """Project/rename saved cells without recomputation or inferred lineage."""
        from analysis_runtime.provenance import fact_view_receipt, record_fact_view, ResultViewFrame
        frame = self.table(fact_id)
        receipt = fact_view_receipt(frame)
        columns = list(frame.columns) if columns is None else list(columns)
        rename = dict(rename or {})
        if len(columns) != len(set(columns)) or not set(columns) <= set(frame.columns) or not set(rename) <= set(columns):
            raise ValueError('View fields must resolve uniquely in the saved result')
        output = ResultViewFrame(frame.loc[:, columns].rename(columns=rename).copy(deep=True))
        if not output.columns.is_unique:
            raise ValueError('View aliases must be unique')
        field_mapping = ({rename.get(field, field): receipt['field_mapping'][field]
                          for field in columns if field in receipt['field_mapping']}
                         if receipt is not None else {})
        if field_mapping:
            record_fact_view(output, receipt['execution_token'], receipt['fact_id'],
                             field_mapping=field_mapping,
                             operation={'kind': 'projection', 'columns': columns, 'rename': rename})
        return output

    def state(self, fact_id):
        """Compatibility alias for status(); it never returns calculated values."""
        return self.status(fact_id)

    def metric_table(self, fact_id, fields: dict[str, str], *, title, row=0,
                     description='', value_precision=None):
        """Display saved cells vertically, retaining each metric's own boundary."""
        from agent.tools import build_table_result
        from analysis_runtime.provenance import fact_view_receipt, record_table_consumption, snapshot_digest
        frame = self.table(fact_id)
        receipt = fact_view_receipt(frame)
        if (type(row) is not int or not 0 <= row < len(frame) or not isinstance(fields, dict)
                or not fields or not set(fields) <= set(frame.columns)):
            raise ValueError('Metric table requires an existing saved row and field-to-label mapping')
        if not receipt or not set(fields) <= set(receipt['field_mapping']):
            raise ValueError('Metric table fields require an actual saved producer receipt')
        rows = [{'metric_field': field, 'metric': label, 'value': frame.iloc[row][field]}
                for field, label in fields.items()]
        output = build_table_result(title=title, description=description, rows=rows,
            columns=[{'key': 'metric', 'label': '指标'}, {'key': 'value', 'label': '数值'}],
            value_precision=value_precision)
        table = output['table']
        binding = {**receipt, 'helper': 'results.metric_table',
                   'helper_options': {'row': row, 'fields': dict(fields), 'value_precision': value_precision},
                   'cell_bindings': [
                       {'field': 'value', 'source_field': receipt['field_mapping'][field],
                        'source_row': row, 'row_selector': {'metric_field': field}}
                       for field in fields]}
        digest = snapshot_digest({'columns': table['columns'], 'rows': table['rows']})
        record_table_consumption(binding, digest)
        table['_semantic_execution_binding'] = {**binding, 'artifact_digest': digest}
        return output

    def chart(self, fact_id, **options):
        return self._execution.chart(self._execution._execute_result(self._definition_id(fact_id)), **options)

    def value(self, fact_id, **options):
        return self._execution.render_value(self._execution._execute_result(self._definition_id(fact_id)), **options)

    def decision_state(self, decision_id):
        return self._execution.decision_state(self._execution.decision(decision_id))

    def matches(self, decision_id):
        return self._execution.matches(self._execution.decision(decision_id))

    def extrema(self, decision_id, label_field, **options):
        return self._execution.render_extrema(self._execution.decision(decision_id), label_field, **options)

    def compose_report(self, parts):
        return self._execution.compose_report(parts)

    def finding(self, finding_id, fact_id, *, kind, field=None, category_field=None, descending=True):
        """Bind descriptive comparisons to saved results, including planned identities."""
        return self._execution.finding(finding_id,
            self._execution._execute_result(self._definition_id(fact_id)), kind=kind,
            field=field, category_field=category_field, descending=descending)

    def source_table(self, source_id):
        return self._execution.source_table(source_id)

    def source_observations(self, source_id):
        return self._execution.source_observations(source_id)


class ExecutablePythonProgram:
    """Native Python definition port, using the same execution engine and receipts."""
    def __init__(self):
        from analysis_runtime.provenance import enable_runtime_evidence
        from analysis_runtime.semantic_execution import SemanticExecution
        enable_runtime_evidence()
        self.execution = SemanticExecution('executable-analysis')
        self._queries = {}
        self._findings = {}
        self._presentation = None

    def register_query(self, identifier: str, loader: Callable[[dict], pd.DataFrame], *, grain=(), on_no_data='raise'):
        if identifier in self._queries or not callable(loader):
            raise ValueError('Source requires a new identity and an actual loader function')
        if on_no_data not in {'raise', 'record'}:
            raise ValueError('on_no_data must be raise or record')
        self._queries[identifier] = (loader, tuple(grain), on_no_data)

    def register_calculation(self, definition: DistinctCount | RowProductSum | PythonCalculation, *, question_id: str, result_id=None):
        if isinstance(definition, PythonCalculation):
            definition = replace(
                definition,
                input_source_ids=tuple(definition.input_source_ids),
                output_grain=tuple(definition.output_grain),
                parameters=deepcopy(dict(definition.parameters)),
            )
        return self.execution.define(definition, question_id=question_id, result_id=result_id)

    def register_transform(self, definition: OneToOneJoin | DecimalColumns):
        return self.execution.define_transform(definition)

    def register_extrema(self, identifier: str, fact_id: str | tuple[str, str], *, mode='max', field=None):
        # Semantic plans name public results; the execution engine owns the
        # calculation IDs. Resolve the same binding used by presentation reads
        # before registering a decision over that result.
        definition_id = (fact_id if isinstance(fact_id, str) and fact_id in self._queries
                         else self.execution.resolve_identity(fact_id))
        if definition_id in self._queries and field is None:
            raise ValueError('Query-source extrema require field=<actual numeric result column>; '
                             'the query identity does not imply a primary value field')
        return self.execution.define_extrema(identifier, definition_id, mode=mode, field=field)

    def register_finding(self, identifier, fact_id, *, kind, field=None,
                         category_field=None, descending=True):
        """Declare requested comparisons independently of chart/table creation.

        The existing finding engine consumes saved results after execution;
        no new calculation, query or semantic assessment is introduced.
        """
        if not isinstance(identifier, str) or not identifier or identifier in self._findings:
            raise ValueError('Finding registration requires a new nonempty identity')
        self._findings[identifier] = (deepcopy(fact_id), {
            'kind': kind, 'field': field, 'category_field': category_field,
            'descending': descending,
        })

    def register_presentation(self, function: Callable[[ExecutableResults, dict], dict]):
        if self._presentation is not None or not callable(function):
            raise ValueError('Program requires one actual presentation function')
        self._presentation = function

    def deliver(self, parameters):
        if self._presentation is None:
            raise ValueError('Register the complete presentation function with program.register_presentation')
        from agent.tools import PhysicalQueryNoDataError, RetryResult, QueryConstraintRetrySignal
        for identifier, (loader, grain, on_no_data) in self._queries.items():
            try:
                frame = loader(deepcopy(parameters))
            except PhysicalQueryNoDataError as error:
                if on_no_data != 'record':
                    raise
                self.execution.add_query_no_data_source(identifier, error.snapshot, grain=grain)
                continue
            if isinstance(frame, RetryResult):
                raise QueryConstraintRetrySignal(frame)
            register_program_source(self.execution, identifier, frame, grain)
        self.execution.run()
        results = ExecutableResults(self.execution)
        for identifier, (fact_id, options) in self._findings.items():
            results.finding(identifier, fact_id, **options)
        return self._presentation(results, deepcopy(parameters))


def validate_python_analysis_definition(source):
    """Validate the definition entry point before adding host execution code."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(f'definition_source syntax error at line {error.lineno}: {error.msg}') from error
    if not any(isinstance(node, ast.FunctionDef) and node.name == 'build_analysis'
               for node in tree.body):
        raise ValueError('definition_source must define build_analysis(program, parameters); '
                         'the host invokes this entry point and saves its registered outputs.')
    # The definition port and the complete-code port have different execution
    # ownership. Running a pasted main here would execute it once before the
    # host main and turn its second query into a misleading identity collision.
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            if (isinstance(called, ast.Name) and called.id in {'build_analysis', 'save_analysis_result'}
                    or isinstance(called, ast.Attribute) and called.attr == 'deliver'):
                raise ValueError(
                    f'definition_source contains an execution entry at line {node.lineno}; '
                    'declare build_analysis and its helpers only. The host creates the program, '
                    'calls build_analysis, delivers it and saves its outputs exactly once. '
                    'Submit an existing complete executable script through code instead.'
                )


def compile_python_analysis_definition(source, *, output_format='standard', defer_presentation=False):
    """Keep the original Python definitions intact; the host owns the main program."""
    validate_python_analysis_definition(source)
    prefix = ['from copy import deepcopy as _definition_copy',
              'from analysis_runtime.executable_program import ExecutablePythonProgram',
              'execution_intent = "analysis"']
    suffix = [
        'analysis_parameters_schema = globals().get("analysis_parameters_schema", {"parameters": [], "defaults": {}})',
        'analysis_parameters = get_analysis_parameters(analysis_parameters_schema.get("defaults", {}))',
        '_program_definition = ExecutablePythonProgram()',
        'build_analysis(_program_definition, _definition_copy(analysis_parameters))',
        '_program_output = _program_definition.deliver(analysis_parameters)',
        (_program_candidate_statement() if defer_presentation else _program_save_statement(output_format)),
    ]
    return '\n'.join([*prefix, source, *suffix]) + '\n'


def _program_candidate_statement():
    """Persist worker calculations and artifacts without treating old prose as final."""
    return (
        'result = save_analysis_result(charts=_program_output.get("charts", []), '
        'tables=_program_output.get("tables", []), defer_presentation=True, '
        'analysis_parameters_schema=analysis_parameters_schema, '
        'analysis_parameters=analysis_parameters)'
    )


def _program_save_statement(output_format):
    if output_format == 'standard':
        output = 'analysis_report=_program_output.get("analysis_report", ""), charts=_program_output.get("charts", []), tables=_program_output.get("tables", [])'
    elif output_format == 'text':
        output = 'output_text=_program_output["output_text"]'
    elif output_format == 'json':
        output = 'output_json=_program_output["output_json"]'
    else:
        raise ValueError('Unknown current output contract')
    return f'result = save_analysis_result({output}, analysis_parameters_schema=analysis_parameters_schema, analysis_parameters=analysis_parameters)'


def register_program_source(execution, identifier, frame, grain):
    try:
        query_receipt(frame)
    except ValueError:
        from analysis_runtime.provenance import query_view_receipt, snapshot_digest
        from agent.tools import CURRENT_QUERY_SNAPSHOTS
        view = query_view_receipt(frame)
        snapshot = (CURRENT_QUERY_SNAPSHOTS.get() or {}).get((view or {}).get('query_key')) or {}
        execution_record = snapshot.get('execution') or {}
        if (view and execution_record.get('status') == 'success'
                and view.get('snapshot_digest') == snapshot_digest(snapshot)):
            execution.add_query_view_source(identifier, frame, view, grain=grain)
            return
        # A custom loader may normalize, page or join physical query results.
        # Retain declared access, never pretend the transformed frame is raw.
        keys = tuple((CURRENT_QUERY_SNAPSHOTS.get() or {}).keys()) or (identifier,)
        execution.add_source(identifier, frame, query_keys=keys, grain=grain)
    else:
        execution.add_query_source(identifier, frame, grain=grain)


