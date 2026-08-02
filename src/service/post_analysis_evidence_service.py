from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from config import Config
from utils import logger
from utils.datasource_utils import safe_json_loads
from utils.llm_model_factory import create_data_insight_model, create_runtime_config


EVIDENCE_PLANNER_PROMPT = """
你是数据洞察结果的业务语义标注者。上游分析已经完成，查询、计算和产物均不可修改。

输入中的 calculation_candidates 已由本地编译器按真实查询与计算血缘确定。你不能选择、合并、拆分或重建计算关系，只需为每个候选补充用户可读的业务语义。

规则：
1. 每个 calculation_id 必须且只能输出一次，不得虚构或遗漏。
2. business_name 应说明业务指标含义，business_explanation 应简洁说明统计对象、范围及计算口径。
3. value_type 只能是 number、integer、currency、percentage、duration、other。
4. display_unit 使用面向用户的展示单位；没有明确单位时为空字符串。
5. 禁止出现 Runtime、Metric、Evidence、AST、API、operation_node_id、query_node_id 等内部术语。
6. 不得输出 SQL、Python、计算结果或 Markdown。

只输出一个 JSON 对象：
{
  "metrics": [
    {
      "calculation_id": "calc_...",
      "metric_key": "稳定英文语义名",
      "business_name": "业务指标名称",
      "business_explanation": "面向用户的统计口径说明",
      "value_type": "number|integer|currency|percentage|duration|other",
      "display_unit": "元/%/台/个/小时等，没有则为空"
    }
  ]
}
""".strip()


class PlannedMetric(BaseModel):
    calculation_id: str
    metric_key: str
    business_name: str
    business_explanation: str
    value_type: Literal["number", "integer", "currency", "percentage", "duration", "other"] = "number"
    display_unit: str = ""


class EvidencePlan(BaseModel):
    metrics: list[PlannedMetric] = Field(default_factory=list)


@dataclass(frozen=True)
class AnalysisExecutionSnapshot:
    execution_id: int
    code_hash: str
    result_hash: str
    generated_code: str
    query_snapshots: dict[str, Any]
    selected_datasource_snapshot: list[dict[str, Any]]
    user_query: str
    analysis_report: str
    charts: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    report_artifact_id: int = 0


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _extract_json_object(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", source, re.DOTALL)
    if fenced:
        source = fenced.group(1)
    else:
        start = source.find("{")
        end = source.rfind("}")
        if start >= 0 and end > start:
            source = source[start:end + 1]
    value = json.loads(source)
    if not isinstance(value, dict):
        raise ValueError("Evidence Planner output must be one JSON object")
    return value


def _artifact_content(artifact: dict[str, Any]) -> dict[str, Any]:
    content = artifact.get("content")
    if isinstance(content, dict):
        return content
    content = safe_json_loads(artifact.get("content_json") or "{}", {})
    return content if isinstance(content, dict) else {}


def _target_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            names.append(item.id)
        elif isinstance(item, ast.Subscript) and isinstance(item.ctx, ast.Store):
            try:
                names.append(ast.unparse(item))
            except Exception:
                continue
    return list(dict.fromkeys(names))


def _loaded_names(node: ast.AST) -> list[str]:
    return list(dict.fromkeys(
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    ))


def _called_operation_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        function = item.func
        if isinstance(function, ast.Name):
            names.add(function.id.lower())
        elif isinstance(function, ast.Attribute):
            names.add(function.attr.lower())
    return names


def _value_fields(chart: dict[str, Any]) -> list[str]:
    document = chart.get("chart_document") if isinstance(chart.get("chart_document"), dict) else chart
    data_contract = chart.get("chart_data_contract") if isinstance(chart.get("chart_data_contract"), dict) else {}
    encoding = document.get("encoding") if isinstance(document.get("encoding"), dict) else {}
    if not encoding and isinstance(data_contract.get("encoding"), dict):
        encoding = data_contract["encoding"]
    values: list[str] = []
    value_field = str(encoding.get("value_field") or "").strip()
    if value_field:
        values.append(value_field)
    raw_fields = encoding.get("value_fields") or []
    if isinstance(raw_fields, (str, dict)):
        raw_fields = [raw_fields]
    for item in raw_fields if isinstance(raw_fields, list) else []:
        if isinstance(item, dict):
            field = item.get("field") or item.get("value_field") or item.get("key")
        else:
            field = item
        field_text = str(field or "").strip()
        if field_text:
            values.append(field_text)
    chart_kind = str(document.get("chart_kind") or chart.get("chart_type") or "").strip().lower()
    if chart_kind == "scatter" and not values:
        for key in ("x_field", "y_field"):
            field_text = str(encoding.get(key) or "").strip()
            if field_text:
                values.append(field_text)
    return list(dict.fromkeys(values))


def _chart_rows(chart: dict[str, Any]) -> list[dict[str, Any]]:
    document = chart.get("chart_document") if isinstance(chart.get("chart_document"), dict) else chart
    dataset = document.get("dataset") if isinstance(document.get("dataset"), dict) else {}
    rows = dataset.get("rows") or []
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _table_column_key(column: Any) -> str:
    if isinstance(column, dict):
        return str(column.get("key") or column.get("field") or column.get("label") or "").strip()
    return str(column or "").strip()


def _is_numeric_value(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_numeric_display_text(value: Any) -> bool:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return False
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1].strip()}"
    try:
        number = Decimal(text)
    except InvalidOperation:
        return False
    return number.is_finite()


class LineageIndexer:
    """Build a conservative code-to-artifact index without inferring business semantics."""

    _OPERATION_CALLS = {
        "groupby", "agg", "aggregate", "sum", "mean", "count", "nunique", "min", "max",
        "value_counts", "sort_values", "head", "tail", "rank", "pct_change", "rolling",
        "resample", "cumsum", "cumcount", "quantile", "merge", "join", "pivot", "pivot_table",
        "len", "size", "idxmin", "idxmax", "to_numeric", "astype", "fillna", "dropna",
        "replace", "rename", "drop_duplicates", "copy",
    }

    def build(self, snapshot: AnalysisExecutionSnapshot) -> dict[str, Any]:
        query_snapshots = self._lineage_query_snapshots(snapshot.query_snapshots)
        (
            operations,
            report_expressions,
            column_aliases,
            chart_sources,
            table_sources,
            table_metric_fields,
        ) = self._index_code(
            snapshot.generated_code,
            query_snapshots,
        )
        operations = [*self._query_operation_nodes(query_snapshots), *operations]
        outputs = self._index_outputs(
            snapshot,
            report_expressions,
            column_aliases=column_aliases,
            chart_sources=chart_sources,
            table_sources=table_sources,
            table_metric_fields=table_metric_fields,
        )
        outputs = self._attach_structured_output_origins(
            outputs,
            code=snapshot.generated_code,
            query_snapshots=query_snapshots,
            operations=operations,
        )
        outputs = self._attach_output_lineage(outputs, operations)
        queries = [
            {
                "query_node_id": str(query_key),
                "query_key": str(query_key),
                "query_scope": self._query_scope(query_snapshot),
            }
            for query_key, query_snapshot in query_snapshots.items()
        ]
        return {
            "code_hash": snapshot.code_hash,
            "result_hash": snapshot.result_hash,
            "query_nodes": queries,
            "operation_nodes": operations,
            "output_nodes": outputs,
        }

    @staticmethod
    def _lineage_query_snapshots(query_snapshots: dict[str, Any]) -> dict[str, Any]:
        successful = {
            str(key): value
            for key, value in query_snapshots.items()
            if isinstance(value, dict)
            and str((value.get("execution") or {}).get("status") or "") == "success"
        }
        return successful or query_snapshots

    @staticmethod
    def _query_operation_nodes(query_snapshots: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for query_key, raw_snapshot in query_snapshots.items():
            snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
            implementation = snapshot.get("implementation") if isinstance(snapshot.get("implementation"), dict) else {}
            operation_payload = {
                "query_key": str(query_key),
                "query_constraints": snapshot.get("query_constraints") or {},
                "sql": implementation.get("sql") or "",
                "loader": implementation.get("loader") or "",
                "datasource_identifier": implementation.get("datasource_identifier") or "",
                "loader_options": implementation.get("loader_options") or {},
                "filter_expressions": implementation.get("filter_expressions") or [],
                "aggregation_hints": implementation.get("aggregation_hints") or [],
                "sql_context": implementation.get("sql_context") or {},
            }
            source = _stable_json(operation_payload)
            nodes.append({
                "operation_node_id": f"op_query_{_digest(source, 12)}",
                "line": 0,
                "kind": "validated_query",
                "semantic_kind": "query",
                "query_node_id": str(query_key),
                "targets": [],
                "source": source[:6000],
            })
            sql_projection_nodes = LineageIndexer._sql_projection_nodes(
                query_key=str(query_key),
                sql=str(implementation.get("sql") or ""),
            )
            nodes.extend(sql_projection_nodes)
            projected_fields = {
                str(field)
                for operation in sql_projection_nodes
                for field in operation.get("targets") or []
                if str(field)
            }
            execution = snapshot.get("execution") if isinstance(snapshot.get("execution"), dict) else {}
            for index, field in enumerate(execution.get("columns") or []):
                field_name = str(field or "").strip()
                if not field_name or field_name in projected_fields:
                    continue
                identity = _stable_json({
                    "query_key": str(query_key),
                    "field": field_name,
                    "source": "execution_schema",
                })
                nodes.append({
                    "operation_node_id": f"op_source_field_{_digest(identity, 12)}",
                    "line": -(len(sql_projection_nodes) + index + 1),
                    "kind": "source_field",
                    "semantic_kind": "projection",
                    "projection_role": "unknown",
                    "query_node_id": str(query_key),
                    "targets": [field_name],
                    "loads": [],
                    "source": field_name,
                })
        return nodes

    @staticmethod
    def _sql_projection_nodes(*, query_key: str, sql: str) -> list[dict[str, Any]]:
        if not sql.strip():
            return []
        try:
            statement = parse_one(sql)
        except ParseError:
            parameterized_sql = re.sub(r"%\([^)]+\)s", "0", sql)
            parameterized_sql = re.sub(r"(?<!%)%s", "0", parameterized_sql)
            try:
                statement = parse_one(parameterized_sql)
            except ParseError:
                return []
        select = statement.find(exp.Select)
        if select is None:
            return []
        group = select.args.get("group")
        group_expressions = list(group.expressions) if isinstance(group, exp.Group) else []
        group_keys = {
            value
            for expression in group_expressions
            for value in (
                expression.sql(pretty=False).strip(),
                str(expression.alias_or_name or "").strip(),
            )
            if value
        }
        nodes: list[dict[str, Any]] = []
        for index, projection in enumerate(select.expressions):
            alias = str(projection.alias or projection.alias_or_name or "").strip()
            if not alias:
                continue
            calculation = projection.this if isinstance(projection, exp.Alias) else projection
            semantic_kind = ""
            if calculation.find(exp.AggFunc) is not None:
                semantic_kind = "aggregation"
            elif calculation.find(exp.Window) is not None:
                semantic_kind = "ranking"
            elif calculation.find(exp.Binary) is not None:
                semantic_kind = "formula"
            else:
                semantic_kind = "projection"
            expression_source = calculation.sql(pretty=False)
            projection_role = "measure"
            if semantic_kind == "projection":
                projection_role = (
                    "dimension"
                    if expression_source.strip() in group_keys or alias in group_keys
                    else "unknown"
                )
            identity = _stable_json({
                "query_key": query_key,
                "alias": alias,
                "expression": expression_source,
            })
            nodes.append({
                "operation_node_id": f"op_query_projection_{_digest(identity, 12)}",
                "line": -(index + 1),
                "kind": "query_projection",
                "semantic_kind": semantic_kind,
                "projection_role": projection_role,
                "query_node_id": query_key,
                "targets": [alias],
                "loads": [],
                "source": expression_source[:2000],
            })
        return nodes

    def _index_code(
        self,
        code: str,
        query_snapshots: dict[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, str]],
        list[str],
        list[str],
        list[set[str]],
    ]:
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return [], [], {}, [], [], []
        operations: list[dict[str, Any]] = []
        report_expressions: list[dict[str, Any]] = []
        query_bindings = self._query_expression_bindings(tree, query_snapshots)
        formatted_numeric_names = self._formatted_numeric_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                source = ast.get_source_segment(code, node) or ast.unparse(node)
                has_operation = bool(_called_operation_names(node) & self._OPERATION_CALLS)
                has_formula = any(isinstance(item, ast.BinOp) for item in ast.walk(node))
                has_formatted_value = any(
                    isinstance(item, ast.FormattedValue) for item in ast.walk(node)
                )
                query_node_id = self._load_query_node_id(node, query_bindings)
                has_projection = self._is_projection_assignment(node)
                if has_operation or has_formula or has_formatted_value or query_node_id or has_projection:
                    semantic_kind = self._operation_semantic_kind(node, query_node_id=query_node_id)
                    node_id = f"op_{getattr(node, 'lineno', 0)}_{_digest(source, 8)}"
                    operations.append({
                        "operation_node_id": node_id,
                        "line": getattr(node, "lineno", 0),
                        "kind": "query_load" if query_node_id else "calculation",
                        "semantic_kind": semantic_kind,
                        "query_node_id": query_node_id or "",
                        "targets": _target_names(node),
                        "loads": _loaded_names(node),
                        "source": source[:1200],
                    })
                operations.extend(self._dataframe_literal_series_operations(node, code))
            if isinstance(node, ast.JoinedStr):
                report_expressions.extend(
                    self._joined_string_expressions(
                        node,
                        code,
                        formatted_numeric_names=formatted_numeric_names,
                    )
                )
        for expression in report_expressions:
            operations.append({
                "operation_node_id": expression["operation_node_id"],
                "line": expression.get("line") or 0,
                "kind": "output_expression",
                "semantic_kind": self._expression_semantic_kind(
                    str(expression.get("expression") or "")
                ),
                "targets": [expression.get("expression") or ""],
                "loads": expression.get("loads") or [],
                "source": expression.get("expression") or "",
            })
        operations.sort(key=lambda item: (int(item.get("line") or 0), str(item.get("operation_node_id"))))
        report_expressions.sort(key=lambda item: (int(item.get("line") or 0), str(item.get("expression_id"))))
        return (
            operations,
            report_expressions,
            self._column_aliases(tree),
            self._chart_data_sources(tree),
            self._table_data_sources(tree),
            self._table_structured_metric_fields(tree),
        )

    @classmethod
    def _dataframe_literal_series_operations(
        cls,
        node: ast.AST,
        code: str,
    ) -> list[dict[str, Any]]:
        value = getattr(node, "value", None)
        targets = _target_names(node)
        if len(targets) != 1 or not isinstance(value, ast.Call):
            return []
        function_name = ""
        if isinstance(value.func, ast.Name):
            function_name = value.func.id
        elif isinstance(value.func, ast.Attribute):
            function_name = value.func.attr
        if function_name != "DataFrame" or not value.args or not isinstance(value.args[0], ast.Dict):
            return []
        frame_name = targets[0]
        operations: list[dict[str, Any]] = []
        for key_node, values_node in zip(value.args[0].keys, value.args[0].values):
            if not (
                isinstance(key_node, ast.Constant)
                and isinstance(key_node.value, str)
                and isinstance(values_node, (ast.List, ast.Tuple))
                and len(values_node.elts) > 1
            ):
                continue
            for index, expression in enumerate(values_node.elts):
                expression_source = ast.get_source_segment(code, expression) or ast.unparse(expression)
                called = _called_operation_names(expression)
                if any(isinstance(item, ast.BinOp) for item in ast.walk(expression)):
                    semantic_kind = "formula"
                elif called.intersection({"sum", "mean", "count", "nunique", "min", "max", "len", "size"}):
                    semantic_kind = "aggregation"
                elif any(isinstance(item, ast.Subscript) for item in ast.walk(expression)):
                    semantic_kind = "transformation"
                else:
                    semantic_kind = "presentation"
                identity = f"{frame_name}:{key_node.value}:{index}:{expression_source}"
                operations.append({
                    "operation_node_id": f"op_series_{getattr(node, 'lineno', 0)}_{_digest(identity, 10)}",
                    "line": getattr(node, "lineno", 0),
                    "kind": "series_value",
                    "semantic_kind": semantic_kind,
                    "query_node_id": "",
                    "targets": [f"{frame_name}[{key_node.value!r}]"],
                    "loads": _loaded_names(expression),
                    "source": expression_source[:1200],
                })
        return operations

    @staticmethod
    def _column_aliases(tree: ast.AST) -> dict[str, dict[str, str]]:
        selected_fields: dict[str, list[str]] = {}
        aliases: dict[str, dict[str, str]] = {}
        assignments = sorted(
            (
                node for node in ast.walk(tree)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
            ),
            key=lambda node: int(getattr(node, "lineno", 0) or 0),
        )
        for node in assignments:
            targets = _target_names(node)
            value = getattr(node, "value", None)
            if len(targets) == 1 and value is not None:
                field_lists = [
                    [str(item.value) for item in subscript.slice.elts]
                    for subscript in ast.walk(value)
                    if isinstance(subscript, ast.Subscript)
                    and isinstance(subscript.slice, (ast.List, ast.Tuple))
                    and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in subscript.slice.elts)
                ]
                if field_lists:
                    selected_fields[targets[0]] = field_lists[0]
                group_fields: list[str] = []
                for call in ast.walk(value):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "groupby"
                        and call.args
                    ):
                        group_node = call.args[0]
                        if isinstance(group_node, ast.Constant) and isinstance(group_node.value, str):
                            group_fields.append(str(group_node.value))
                        elif isinstance(group_node, (ast.List, ast.Tuple)):
                            group_fields.extend(
                                str(item.value)
                                for item in group_node.elts
                                if isinstance(item, ast.Constant) and isinstance(item.value, str)
                            )
                aggregation_fields: list[str] = []
                for call in ast.walk(value):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in {"agg", "aggregate"}
                        and call.args
                        and isinstance(call.args[0], ast.Dict)
                    ):
                        continue
                    aggregation_fields = [
                        str(key.value)
                        for key in call.args[0].keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    ]
                    if aggregation_fields:
                        break
                if aggregation_fields:
                    # Preserve the structural source-to-display mapping for the
                    # common ``groupby(...).agg(...); frame.columns = [...]`` form.
                    selected_fields[targets[0]] = list(dict.fromkeys([
                        *group_fields,
                        *aggregation_fields,
                    ]))
            raw_targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            for target in raw_targets:
                if not (
                    isinstance(target, ast.Attribute)
                    and target.attr == "columns"
                    and isinstance(target.value, ast.Name)
                    and isinstance(value, (ast.List, ast.Tuple))
                    and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts)
                ):
                    continue
                frame_name = target.value.id
                old_fields = selected_fields.get(frame_name) or []
                new_fields = [str(item.value) for item in value.elts]
                if len(old_fields) == len(new_fields):
                    aliases[frame_name] = dict(zip(new_fields, old_fields))
            for target in raw_targets:
                if not (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and value is not None
                ):
                    continue
                frame_name = target.value.id
                source_fields = [
                    str(item.slice.value)
                    for item in ast.walk(value)
                    if isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Name)
                    and item.value.id == frame_name
                    and isinstance(item.slice, ast.Constant)
                    and isinstance(item.slice.value, str)
                ]
                if source_fields:
                    aliases.setdefault(frame_name, {})[str(target.slice.value)] = source_fields[0]
            if len(targets) == 1 and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr == "rename" and isinstance(value.func.value, ast.Name):
                    columns_node = next((item.value for item in value.keywords if item.arg == "columns"), None)
                    if isinstance(columns_node, ast.Dict):
                        for old_node, new_node in zip(columns_node.keys, columns_node.values):
                            if (
                                isinstance(old_node, ast.Constant) and isinstance(old_node.value, str)
                                and isinstance(new_node, ast.Constant) and isinstance(new_node.value, str)
                            ):
                                aliases.setdefault(targets[0], {})[str(new_node.value)] = str(old_node.value)
        for call in (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "build_table_result")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "build_table_result")
            )
        ):
            rows_expression = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "rows"),
                None,
            )
            if not isinstance(rows_expression, ast.ListComp) or not isinstance(rows_expression.elt, ast.Dict):
                continue
            source_frame = LineageIndexer._list_comprehension_source_frame(rows_expression)
            row_names = LineageIndexer._list_comprehension_row_names(rows_expression)
            if not source_frame or not row_names:
                continue
            for key_node, value_node in zip(rows_expression.elt.keys, rows_expression.elt.values):
                if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                    continue
                source_fields = [
                    str(item.slice.value)
                    for item in ast.walk(value_node)
                    if isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Name)
                    and item.value.id in row_names
                    and isinstance(item.slice, ast.Constant)
                    and isinstance(item.slice.value, str)
                ]
                if source_fields:
                    aliases.setdefault(source_frame, {})[str(key_node.value)] = source_fields[0]
        return aliases

    @staticmethod
    def _list_comprehension_source_frame(expression: ast.ListComp) -> str:
        for generator in expression.generators:
            iterator = generator.iter
            if (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Attribute)
                and iterator.func.attr in {"iterrows", "itertuples"}
                and isinstance(iterator.func.value, ast.Name)
            ):
                return iterator.func.value.id
        return ""

    @staticmethod
    def _list_comprehension_row_names(expression: ast.ListComp) -> set[str]:
        names: set[str] = set()
        for generator in expression.generators:
            target = generator.target
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                names.update(
                    item.id for item in target.elts if isinstance(item, ast.Name)
                )
        return names

    @staticmethod
    def _chart_data_sources(tree: ast.AST) -> list[str]:
        calls = sorted(
            (
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id in {
                        "build_chart_result", "build_multi_metric_chart_result", "build_chart_suite",
                    })
                    or (isinstance(node.func, ast.Attribute) and node.func.attr in {
                        "build_chart_result", "build_multi_metric_chart_result", "build_chart_suite",
                    })
                )
            ),
            key=lambda node: int(getattr(node, "lineno", 0) or 0),
        )
        sources: list[str] = []
        for call in calls:
            data_expression = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "data"),
                call.args[0] if call.args else None,
            )
            loaded = _loaded_names(data_expression) if data_expression is not None else []
            sources.append(loaded[0] if loaded else "")
        return sources

    @staticmethod
    def _table_data_sources(tree: ast.AST) -> list[str]:
        calls = sorted(
            (
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "build_table_result")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "build_table_result")
                )
            ),
            key=lambda node: int(getattr(node, "lineno", 0) or 0),
        )
        sources: list[str] = []
        for call in calls:
            rows_expression = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "rows"),
                None,
            )
            source = ""
            if isinstance(rows_expression, ast.ListComp):
                source = LineageIndexer._list_comprehension_source_frame(rows_expression)
            elif (
                isinstance(rows_expression, ast.Call)
                and isinstance(rows_expression.func, ast.Attribute)
                and rows_expression.func.attr == "to_dict"
                and isinstance(rows_expression.func.value, ast.Name)
            ):
                source = rows_expression.func.value.id
            if not source:
                loaded = _loaded_names(rows_expression) if rows_expression is not None else []
                source = loaded[0] if loaded else ""
            sources.append(source)
        return sources

    @staticmethod
    def _table_structured_metric_fields(tree: ast.AST) -> list[set[str]]:
        calls = sorted(
            (
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "build_table_result")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "build_table_result")
                )
            ),
            key=lambda node: int(getattr(node, "lineno", 0) or 0),
        )
        result: list[set[str]] = []
        for call in calls:
            rows_expression = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "rows"),
                None,
            )
            metric_fields: set[str] = set()
            if isinstance(rows_expression, ast.ListComp) and isinstance(rows_expression.elt, ast.Dict):
                row_names = LineageIndexer._list_comprehension_row_names(rows_expression)
                for key_node, value_node in zip(rows_expression.elt.keys, rows_expression.elt.values):
                    if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                        continue
                    reads_row = any(
                        isinstance(item, ast.Subscript)
                        and isinstance(item.value, ast.Name)
                        and item.value.id in row_names
                        for item in ast.walk(value_node)
                    )
                    is_presentation_or_calculation = isinstance(
                        value_node,
                        (ast.Call, ast.JoinedStr, ast.BinOp, ast.UnaryOp),
                    )
                    if reads_row and is_presentation_or_calculation:
                        metric_fields.add(str(key_node.value))
            result.append(metric_fields)
        return result

    @classmethod
    def _expression_semantic_kind(cls, expression: str) -> str:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            return "presentation"
        called = _called_operation_names(tree)
        if called.intersection({"len", "size"}):
            return "aggregation"
        return "presentation"

    @classmethod
    def _operation_semantic_kind(cls, node: ast.AST, *, query_node_id: str = "") -> str:
        if query_node_id:
            return "query_load"
        called = _called_operation_names(node)
        if any(isinstance(item, ast.BinOp) for item in ast.walk(node)):
            return "formula"
        if called.intersection({
            "sum", "mean", "count", "nunique", "min", "max", "len", "size",
            "agg", "aggregate", "value_counts", "quantile", "cumsum", "cumcount",
        }):
            return "aggregation"
        if called.intersection({"sort_values", "head", "tail", "rank", "idxmin", "idxmax"}):
            return "ranking"
        if cls._contains_relational_join(node):
            return "join"
        if called.intersection({"groupby", "pivot", "pivot_table", "resample", "rolling"}):
            return "grouping"
        return "transformation"

    @staticmethod
    def _contains_relational_join(node: ast.AST) -> bool:
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name) and call.func.id == "merge":
                return True
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "merge":
                return True
            if call.func.attr != "join":
                continue
            receiver = call.func.value
            if isinstance(receiver, (ast.Constant, ast.JoinedStr, ast.List, ast.Tuple, ast.Dict, ast.Set)):
                continue
            return True
        return False

    @staticmethod
    def _is_projection_assignment(node: ast.AST) -> bool:
        value = getattr(node, "value", None)
        raw_targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
        return (
            value is not None
            and any(isinstance(target, ast.Name) for target in raw_targets)
            and any(isinstance(item, ast.Subscript) for item in ast.walk(value))
        )

    @staticmethod
    def _query_expression_bindings(
        tree: ast.AST,
        query_snapshots: dict[str, Any],
    ) -> dict[str, str]:
        bindings: dict[str, str] = {}
        sql_assignments: dict[str, ast.AST] = {}
        static_values: dict[str, Any] = {}

        def static_value(node: ast.AST | None) -> Any:
            if node is None:
                raise ValueError("missing expression")
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name) and node.id in static_values:
                return static_values[node.id]
            if isinstance(node, (ast.Dict, ast.List, ast.Tuple)):
                return ast.literal_eval(node)
            if isinstance(node, ast.Subscript):
                container = static_value(node.value)
                key = static_value(node.slice)
                return container[key]
            if isinstance(node, ast.BinOp):
                left = static_value(node.left)
                right = static_value(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                raise ValueError("unsupported binary operation")
            if isinstance(node, ast.JoinedStr):
                parts: list[str] = []
                for item in node.values:
                    if isinstance(item, ast.Constant):
                        parts.append(str(item.value))
                    elif isinstance(item, ast.FormattedValue):
                        parts.append(str(static_value(item.value)))
                    else:
                        raise ValueError("unsupported formatted value")
                return "".join(parts)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_analysis_parameters"
                and node.args
            ):
                return static_value(node.args[0])
            raise ValueError("expression is not statically resolvable")

        assignments = sorted(
            (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
            key=lambda node: int(getattr(node, "lineno", 0) or 0),
        )
        for node in assignments:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = getattr(node, "value", None)
            for target in _target_names(node):
                if isinstance(value, (ast.Constant, ast.JoinedStr)):
                    sql_assignments[target] = value
                if "[" in target:
                    continue
                try:
                    static_values[target] = static_value(value)
                except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
                    pass

        query_keys_by_sql_hash = {
            str((snapshot.get("implementation") or {}).get("sql_hash") or ""): str(query_key)
            for query_key, snapshot in query_snapshots.items()
            if isinstance(snapshot, dict)
            and isinstance(snapshot.get("implementation"), dict)
            and str((snapshot.get("implementation") or {}).get("sql_hash") or "")
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            keywords = {str(item.arg or ""): item.value for item in node.keywords}
            if function_name == "validate_query_constraints":
                query_key_node = keywords.get("query_key")
                if not isinstance(query_key_node, ast.Constant) or not isinstance(query_key_node.value, str):
                    continue
                query_key = str(query_key_node.value).strip()
                if not query_key or query_key not in query_snapshots:
                    continue
                for name in ("sql", "filter_expressions", "sql_context"):
                    expression = keywords.get(name)
                    if expression is not None:
                        bindings[ast.dump(expression, include_attributes=False)] = query_key
                continue
            if function_name in {"load_local_file", "load_local_file_low_memory"}:
                identifier_expression = keywords.get("file_path") or (node.args[0] if node.args else None)
                resolved_identifier = (
                    sql_assignments.get(identifier_expression.id)
                    if isinstance(identifier_expression, ast.Name)
                    else identifier_expression
                )
                identifier = (
                    str(resolved_identifier.value)
                    if isinstance(resolved_identifier, ast.Constant)
                    and isinstance(resolved_identifier.value, str)
                    else ""
                )
                matching_keys = [
                    str(key)
                    for key, snapshot in query_snapshots.items()
                    if isinstance(snapshot, dict)
                    and str((snapshot.get("implementation") or {}).get("datasource_identifier") or "") == identifier
                ]
                if len(matching_keys) == 1 and identifier_expression is not None:
                    bindings[ast.dump(identifier_expression, include_attributes=False)] = matching_keys[0]
                continue
            if function_name != "load_data_with_sql":
                continue
            sql_expression = keywords.get("sql") or (node.args[0] if node.args else None)
            resolved_expression = (
                sql_assignments.get(sql_expression.id)
                if isinstance(sql_expression, ast.Name)
                else sql_expression
            )
            if resolved_expression is None or sql_expression is None:
                continue
            query_key = ""
            resolved_sql = None
            try:
                resolved_sql = static_value(resolved_expression)
            except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
                resolved_sql = None
            if isinstance(resolved_sql, str):
                sql_hash = hashlib.sha256(resolved_sql.encode("utf-8")).hexdigest()
                query_key = query_keys_by_sql_hash.get(sql_hash, "")
            elif isinstance(resolved_expression, ast.JoinedStr):
                matching_keys = [
                    str(key)
                    for key, snapshot in query_snapshots.items()
                    if isinstance(snapshot, dict)
                    and LineageIndexer._joined_sql_matches(
                        resolved_expression,
                        str((snapshot.get("implementation") or {}).get("sql") or ""),
                    )
                ]
                if len(matching_keys) == 1:
                    query_key = matching_keys[0]
            if query_key:
                bindings[ast.dump(sql_expression, include_attributes=False)] = query_key
        return bindings

    @staticmethod
    def _joined_sql_matches(expression: ast.JoinedStr, sql: str) -> bool:
        if not str(sql or "").strip():
            return False
        pattern_parts: list[str] = []
        for item in expression.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                pattern_parts.append(re.escape(item.value))
            elif isinstance(item, ast.FormattedValue):
                pattern_parts.append(r".+?")
            else:
                return False
        return re.fullmatch("".join(pattern_parts), sql, flags=re.DOTALL) is not None

    @staticmethod
    def _load_query_node_id(node: ast.AST, query_bindings: dict[str, str]) -> str:
        value = getattr(node, "value", None)
        if value is None:
            return ""
        for call in ast.walk(value):
            if not isinstance(call, ast.Call):
                continue
            function_name = ""
            if isinstance(call.func, ast.Name):
                function_name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                function_name = call.func.attr
            if function_name not in {
                "load_data_with_sql", "load_local_file", "load_local_file_low_memory",
                "load_minio_file", "load_data_with_api",
            }:
                continue
            access_arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            for argument in access_arguments:
                signature = ast.dump(argument, include_attributes=False)
                query_key = query_bindings.get(signature)
                if query_key:
                    return query_key
        return ""

    @staticmethod
    def _format_spec_text(node: ast.AST | None) -> str:
        if not isinstance(node, ast.JoinedStr):
            return ""
        return "".join(
            str(item.value)
            for item in node.values
            if isinstance(item, ast.Constant)
        )

    @classmethod
    def _formatted_numeric_names(cls, tree: ast.AST) -> dict[str, str]:
        names: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = getattr(node, "value", None)
            if not isinstance(value, ast.JoinedStr):
                continue
            formatted = [
                item for item in value.values if isinstance(item, ast.FormattedValue)
            ]
            if not formatted:
                continue
            format_specs = [cls._format_spec_text(item.format_spec) for item in formatted]
            if not any(re.search(r"[bcdoxXneEfFgG%]", spec) for spec in format_specs):
                continue
            suffix = "%" if any("%" in spec for spec in format_specs) else ""
            raw_targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            for target in raw_targets:
                if isinstance(target, ast.Name):
                    names[target.id] = suffix
        return names

    @classmethod
    def _joined_string_expressions(
        cls,
        node: ast.JoinedStr,
        code: str,
        *,
        formatted_numeric_names: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        values = list(node.values)
        formatted_numeric_names = formatted_numeric_names or {}
        template_source = ast.get_source_segment(code, node) or ast.unparse(node)
        template_structure = ast.dump(node, include_attributes=False)
        template_id = (
            f"template_{getattr(node, 'lineno', 0)}_"
            f"{getattr(node, 'col_offset', 0)}_{_digest(template_structure, 10)}"
        )
        template_tokens: list[dict[str, Any]] = []
        slot_index = 0
        for value in values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                template_tokens.append({"literal": value.value})
            elif isinstance(value, ast.FormattedValue):
                format_spec = cls._format_spec_text(value.format_spec)
                preformatted_suffix = (
                    formatted_numeric_names.get(value.value.id, "")
                    if isinstance(value.value, ast.Name)
                    else ""
                )
                numeric_hint = bool(format_spec) or isinstance(
                    value.value,
                    (ast.BinOp, ast.UnaryOp),
                ) or (
                    isinstance(value.value, ast.Call)
                    and isinstance(value.value.func, ast.Name)
                    and value.value.func.id in {"abs", "float", "int", "len", "round", "sum"}
                ) or bool(preformatted_suffix)
                template_tokens.append({
                    "slot": slot_index,
                    "numeric_hint": numeric_hint,
                    "numeric_suffix": "%" if "%" in format_spec else preformatted_suffix,
                })
                slot_index += 1
        expressions: list[dict[str, Any]] = []
        slot_index = 0
        for index, item in enumerate(values):
            if not isinstance(item, ast.FormattedValue):
                continue
            slot_token = next(
                token
                for token in template_tokens
                if token.get("slot") == slot_index
            )
            expression = ast.get_source_segment(code, item.value) or ast.unparse(item.value)
            prefix = values[index - 1].value if index > 0 and isinstance(values[index - 1], ast.Constant) else ""
            suffix = values[index + 1].value if index + 1 < len(values) and isinstance(values[index + 1], ast.Constant) else ""
            prefix_text = str(prefix or "")[-100:]
            suffix_text = str(suffix or "")[:100]
            expressions.append({
                "expression_id": f"expr_{getattr(item, 'lineno', 0)}_{_digest(expression + prefix_text + suffix_text, 8)}",
                "operation_node_id": f"op_expr_{getattr(item, 'lineno', 0)}_{_digest(expression, 8)}",
                "line": getattr(item, "lineno", 0),
                "expression": expression,
                "loads": _loaded_names(item.value),
                "prefix": prefix_text,
                "suffix": suffix_text,
                "template_id": template_id,
                "template_tokens": template_tokens,
                "slot_index": slot_index,
                "numeric_hint": bool(slot_token.get("numeric_hint")),
            })
            slot_index += 1
        return expressions

    def _index_outputs(
        self,
        snapshot: AnalysisExecutionSnapshot,
        report_expressions: list[dict[str, Any]],
        *,
        column_aliases: dict[str, dict[str, str]],
        chart_sources: list[str],
        table_sources: list[str],
        table_metric_fields: list[set[str]],
    ) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for chart_index, chart in enumerate(snapshot.charts):
            rows = _chart_rows(chart)
            data_contract = (
                chart.get("chart_data_contract")
                if isinstance(chart.get("chart_data_contract"), dict)
                else {}
            )
            source_query_node_id = str(data_contract.get("source_query_key") or "").strip()
            source_frame = chart_sources[chart_index] if len(chart_sources) == len(snapshot.charts) else ""
            for field in _value_fields(chart):
                samples = [row.get(field) for row in rows[:3] if field in row]
                title = str(chart.get("title") or "").strip()
                source_field = str((column_aliases.get(source_frame) or {}).get(field) or "").strip()
                outputs.append({
                    "output_node_id": f"out_chart_{chart_index}_{_digest(field, 8)}",
                    "artifact_type": "chart",
                    "artifact_index": chart_index,
                    "artifact_id": int(chart.get("_artifact_id") or 0),
                    "field": field,
                    "label": f"{title} ({field})" if title else field,
                    "sample_values": samples,
                    "source_query_node_id": source_query_node_id,
                    "source_frame": source_frame,
                    "lineage_fields": [source_field] if source_field else [field],
                })

        for table_index, table in enumerate(snapshot.tables):
            rows = table.get("rows") or []
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            source_frame = table_sources[table_index] if len(table_sources) == len(snapshot.tables) else ""
            structured_metric_fields = (
                table_metric_fields[table_index]
                if len(table_metric_fields) == len(snapshot.tables)
                else set()
            )
            for column_index, column in enumerate(table.get("columns") or []):
                key = _table_column_key(column)
                if not key:
                    continue
                sample_values = [row.get(key) for row in rows[:3] if key in row]
                if key not in structured_metric_fields and not any(
                    _is_numeric_value(value) or _is_numeric_display_text(str(value).rstrip("%"))
                    for value in sample_values
                ):
                    continue
                label = str(column.get("label") or key) if isinstance(column, dict) else key
                source_field = str((column_aliases.get(source_frame) or {}).get(key) or "").strip()
                outputs.append({
                    "output_node_id": f"out_table_{table_index}_{_digest(key, 8)}",
                    "artifact_type": "table",
                    "artifact_index": table_index,
                    "artifact_id": int(table.get("_artifact_id") or 0),
                    "field": key,
                    "column_index": column_index,
                    "label": label,
                    "sample_values": sample_values,
                    "source_frame": source_frame,
                    "lineage_fields": [source_field] if source_field else [key],
                })

        for output in self._locate_report_outputs(snapshot.analysis_report, report_expressions):
            if self._is_known_dimension_output(output, snapshot.selected_datasource_snapshot):
                continue
            output["artifact_id"] = snapshot.report_artifact_id
            outputs.append(output)
        return outputs

    @classmethod
    def _attach_structured_output_origins(
        cls,
        outputs: list[dict[str, Any]],
        *,
        code: str,
        query_snapshots: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach column/scalar lineage before the legacy name-level fallback runs."""
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return outputs

        operation_by_id = {
            str(item.get("operation_node_id")): item
            for item in operations
            if str(item.get("operation_node_id") or "")
        }
        operations_by_line: dict[int, list[dict[str, Any]]] = {}
        for operation in operation_by_id.values():
            operations_by_line.setdefault(int(operation.get("line") or 0), []).append(operation)

        def empty_origin() -> dict[str, set[str]]:
            return {"queries": set(), "boundaries": set(), "operations": set()}

        def merge_origins(*items: dict[str, set[str]] | None) -> dict[str, set[str]]:
            merged = empty_origin()
            for item in items:
                if not item:
                    continue
                for key in merged:
                    merged[key].update(item.get(key) or set())
            return merged

        def operation_for(node: ast.AST) -> dict[str, Any] | None:
            source = ast.get_source_segment(code, node) or ast.unparse(node)
            candidates = operations_by_line.get(int(getattr(node, "lineno", 0) or 0), [])
            return next((item for item in candidates if str(item.get("source") or "").strip() == source.strip()), None)

        query_projection_origins: dict[str, dict[str, dict[str, set[str]]]] = {}
        for operation_id, operation in operation_by_id.items():
            if operation.get("kind") not in {"query_projection", "source_field"}:
                continue
            query_id = str(operation.get("query_node_id") or "").strip()
            for field in operation.get("targets") or []:
                query_projection_origins.setdefault(query_id, {})[str(field)] = {
                    "queries": {query_id},
                    "boundaries": {operation_id},
                    "operations": {operation_id},
                }

        query_bindings = cls._query_expression_bindings(tree, query_snapshots)
        frame_columns: dict[str, dict[str, dict[str, set[str]]]] = {}
        frame_positions: dict[str, list[dict[str, set[str]]]] = {}
        scalar_origins: dict[str, dict[str, set[str]]] = {}
        row_frames: list[tuple[str, str, int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.comprehension)):
                continue
            iterator = node.iter
            source_frame = ""
            if (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Attribute)
                and iterator.func.attr in {"iterrows", "itertuples"}
                and isinstance(iterator.func.value, ast.Name)
            ):
                source_frame = iterator.func.value.id
            if not source_frame:
                continue
            targets = []
            if isinstance(node.target, ast.Name):
                targets = [node.target.id]
            elif isinstance(node.target, (ast.Tuple, ast.List)):
                targets = [item.id for item in node.target.elts if isinstance(item, ast.Name)]
            start = int(getattr(node, "lineno", 0) or 0)
            end = int(getattr(node, "end_lineno", start) or start)
            for target in targets:
                if target != "_":
                    row_frames.append((target, source_frame, start, end))
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            if not (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "apply"
                and isinstance(call.func.value, ast.Name)
                and call.args
                and isinstance(call.args[0], ast.Lambda)
            ):
                continue
            lambda_node = call.args[0]
            start = int(getattr(lambda_node, "lineno", getattr(call, "lineno", 0)) or 0)
            end = int(getattr(lambda_node, "end_lineno", getattr(call, "end_lineno", start)) or start)
            for argument in lambda_node.args.args:
                row_frames.append((argument.arg, call.func.value.id, start, end))

        def row_source(name: str, line: int) -> str:
            matches = [
                (end - start, frame)
                for alias, frame, start, end in row_frames
                if alias == name and start <= line <= end
            ]
            return min(matches, default=(0, ""))[1]

        def subscript_key(node: ast.Subscript) -> str:
            value = node.slice
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return str(value.value)
            return ""

        def expression_origin(node: ast.AST | None, line: int) -> dict[str, set[str]]:
            if node is None:
                return empty_origin()
            origins: list[dict[str, set[str]]] = []
            for item in ast.walk(node):
                if isinstance(item, ast.Subscript) and isinstance(item.value, ast.Name):
                    field = subscript_key(item)
                    if not field:
                        continue
                    frame_name = item.value.id
                    frame_name = (
                        row_source(frame_name, line)
                        or row_source(
                            frame_name,
                            int(getattr(item, "lineno", line) or line),
                        )
                        or frame_name
                    )
                    origin = (frame_columns.get(frame_name) or {}).get(field)
                    if origin:
                        origins.append(origin)
                elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
                    origin = scalar_origins.get(item.id)
                    if origin:
                        origins.append(origin)
            return merge_origins(*origins)

        def frame_name_from_expression(node: ast.AST | None) -> str:
            if isinstance(node, ast.Name) and node.id in frame_columns:
                return node.id
            if isinstance(node, ast.Subscript):
                return frame_name_from_expression(node.value)
            if isinstance(node, ast.Attribute):
                return frame_name_from_expression(node.value)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    name = frame_name_from_expression(node.func.value)
                    if name:
                        return name
                for argument in node.args:
                    name = frame_name_from_expression(argument)
                    if name:
                        return name
            return ""

        def copy_frame(target: str, source: str) -> None:
            frame_columns[target] = {
                field: merge_origins(origin)
                for field, origin in (frame_columns.get(source) or {}).items()
            }
            frame_positions[target] = [
                merge_origins(origin) for origin in frame_positions.get(source) or []
            ]

        assignments = sorted(
            (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
            key=lambda node: int(getattr(node, "lineno", 0) or 0),
        )
        for node in assignments:
            value = getattr(node, "value", None)
            line = int(getattr(node, "lineno", 0) or 0)
            raw_targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            simple_targets = [target.id for target in raw_targets if isinstance(target, ast.Name)]

            if len(simple_targets) == 1:
                target = simple_targets[0]
                query_id = cls._load_query_node_id(node, query_bindings)
                if query_id:
                    projections = query_projection_origins.get(query_id) or {}
                    columns = list(projections)
                    if not columns:
                        snapshot = query_snapshots.get(query_id) if isinstance(query_snapshots.get(query_id), dict) else {}
                        execution = snapshot.get("execution") if isinstance(snapshot.get("execution"), dict) else {}
                        columns = [str(item) for item in execution.get("columns") or [] if str(item)]
                    frame_columns[target] = {
                        column: merge_origins(
                            projections.get(column),
                            {"queries": {query_id}, "boundaries": set(), "operations": set()},
                        )
                        for column in columns
                    }
                    frame_positions[target] = [frame_columns[target][column] for column in columns]
                    continue

                if (
                    isinstance(value, ast.Call)
                    and ((isinstance(value.func, ast.Name) and value.func.id == "DataFrame")
                         or (isinstance(value.func, ast.Attribute) and value.func.attr == "DataFrame"))
                    and value.args
                    and isinstance(value.args[0], ast.Dict)
                ):
                    mapped: dict[str, dict[str, set[str]]] = {}
                    positions: list[dict[str, set[str]]] = []
                    for key_node, values_node in zip(value.args[0].keys, value.args[0].values):
                        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                            continue
                        origin = expression_origin(values_node, line)
                        mapped[str(key_node.value)] = origin
                        positions.append(origin)
                    frame_columns[target] = mapped
                    frame_positions[target] = positions
                    continue

                merge_call = next(
                    (
                        call for call in ast.walk(value) if isinstance(call, ast.Call)
                        and ((isinstance(call.func, ast.Name) and call.func.id == "merge")
                             or (isinstance(call.func, ast.Attribute) and call.func.attr == "merge"))
                    ),
                    None,
                ) if value is not None else None
                if merge_call is not None:
                    source_frames = list(dict.fromkeys(
                        name for name in (
                            frame_name_from_expression(merge_call.func.value) if isinstance(merge_call.func, ast.Attribute) else "",
                            *(frame_name_from_expression(argument) for argument in merge_call.args),
                        ) if name
                    ))
                    merged_columns: dict[str, dict[str, set[str]]] = {}
                    merged_positions: list[dict[str, set[str]]] = []
                    for source in source_frames:
                        for field, origin in (frame_columns.get(source) or {}).items():
                            merged_columns[field] = merge_origins(merged_columns.get(field), origin)
                        merged_positions.extend(merge_origins(item) for item in frame_positions.get(source) or [])
                    frame_columns[target] = merged_columns
                    frame_positions[target] = merged_positions
                    continue

                source_frame = frame_name_from_expression(value)
                is_frame_transform = bool(source_frame) and (
                    any(
                        isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr in {
                            "copy", "sort_values", "head", "tail", "nlargest", "nsmallest",
                            "dropna", "fillna", "reset_index", "rename", "groupby", "agg", "aggregate",
                        }
                        for item in ast.walk(value)
                    )
                    or any(
                        isinstance(item, ast.Subscript)
                        and frame_name_from_expression(item.value) == source_frame
                        and not subscript_key(item)
                        for item in ast.walk(value)
                    )
                )
                if is_frame_transform:
                    copy_frame(target, source_frame)
                    selected_fields = next(
                        (
                            [str(item.value) for item in subscript.slice.elts]
                            for subscript in ast.walk(value)
                            if isinstance(subscript, ast.Subscript)
                            and frame_name_from_expression(subscript.value) == source_frame
                            and isinstance(subscript.slice, (ast.List, ast.Tuple))
                            and all(
                                isinstance(item, ast.Constant) and isinstance(item.value, str)
                                for item in subscript.slice.elts
                            )
                        ),
                        [],
                    )
                    if selected_fields:
                        frame_columns[target] = {
                            field: merge_origins((frame_columns.get(source_frame) or {}).get(field))
                            for field in selected_fields
                            if field in (frame_columns.get(source_frame) or {})
                        }
                        frame_positions[target] = [
                            frame_columns[target][field]
                            for field in selected_fields
                            if field in frame_columns[target]
                        ]
                    continue

                origin = expression_origin(value, line)
                operation = operation_for(node)
                if operation and origin["queries"]:
                    operation_id = str(operation.get("operation_node_id") or "")
                    origin["operations"].add(operation_id)
                    if operation.get("semantic_kind") in {"aggregation", "formula", "ranking", "projection"}:
                        origin["boundaries"] = {operation_id}
                if origin["queries"]:
                    scalar_origins[target] = origin

            for target_node in raw_targets:
                if isinstance(target_node, ast.Subscript) and isinstance(target_node.value, ast.Name):
                    frame_name = target_node.value.id
                    field = subscript_key(target_node)
                    if not field:
                        continue
                    origin = expression_origin(value, line)
                    operation = operation_for(node)
                    if operation and origin["queries"]:
                        operation_id = str(operation.get("operation_node_id") or "")
                        origin["operations"].add(operation_id)
                        if operation.get("semantic_kind") in {"aggregation", "formula", "ranking", "projection"}:
                            origin["boundaries"] = {operation_id}
                    if origin["queries"]:
                        frame_columns.setdefault(frame_name, {})[field] = origin
                        existing_order = list(frame_columns[frame_name])
                        frame_positions[frame_name] = [frame_columns[frame_name][name] for name in existing_order]
                elif (
                    isinstance(target_node, ast.Attribute)
                    and target_node.attr == "columns"
                    and isinstance(target_node.value, ast.Name)
                    and isinstance(value, (ast.List, ast.Tuple))
                ):
                    frame_name = target_node.value.id
                    old_positions = list(frame_positions.get(frame_name) or [])
                    if len(old_positions) != len(value.elts):
                        continue
                    names = [
                        str(item.value) if isinstance(item, ast.Constant) and isinstance(item.value, str) else ""
                        for item in value.elts
                    ]
                    if all(names):
                        frame_columns[frame_name] = {
                            name: old_positions[index] for index, name in enumerate(names)
                        }
                    frame_positions[frame_name] = old_positions

        def family_signature(operation_id: str) -> str:
            operation = operation_by_id.get(operation_id) or {}
            source = str(operation.get("source") or "").strip()
            if operation.get("kind") in {"query_projection", "source_field"} and source:
                try:
                    expression = parse_one(f"SELECT {source}").expressions[0]
                    expression = expression.transform(
                        lambda item: exp.Literal.string("?") if isinstance(item, exp.Literal) else item
                    )
                    source = expression.sql(pretty=False)
                except Exception:
                    source = re.sub(r"(['\"]).*?\1|\b\d+(?:\.\d+)?\b", "?", source)
            return _stable_json({
                "kind": operation.get("semantic_kind") or "",
                "source": source,
            })

        enriched: list[dict[str, Any]] = []
        for raw_output in outputs:
            output = dict(raw_output)
            source_frame = str(output.get("source_frame") or "").strip()
            origin: dict[str, set[str]] | None = None
            if source_frame:
                origin = (frame_columns.get(source_frame) or {}).get(str(output.get("field") or ""))
                if origin is None and output.get("artifact_type") == "table":
                    position = int(output.get("column_index") or 0)
                    positions = frame_positions.get(source_frame) or []
                    if 0 <= position < len(positions):
                        origin = positions[position]
            elif output.get("artifact_type") == "report":
                try:
                    expression = ast.parse(str(output.get("field") or ""), mode="eval")
                    origin = expression_origin(expression, int(output.get("source_line") or 0))
                except SyntaxError:
                    origin = None
            if origin and origin.get("queries") and origin.get("boundaries"):
                boundary_ids = sorted(origin["boundaries"])
                families = {family_signature(item) for item in boundary_ids}
                output["structured_origin"] = True
                output["candidate_query_node_ids"] = sorted(origin["queries"])
                output["boundary_operation_node_ids"] = boundary_ids
                output["candidate_operation_node_ids"] = sorted(
                    set(origin["operations"]) | set(boundary_ids)
                )
                output["direct_operation_node_ids"] = boundary_ids
                output["ambiguous_calculation_boundary"] = len(families) > 1
            enriched.append(output)
        return enriched

    @staticmethod
    def _is_known_dimension_output(
        output: dict[str, Any],
        datasource_snapshot: list[dict[str, Any]],
    ) -> bool:
        field_names = [str(item) for item in output.get("lineage_fields") or [] if str(item)]
        if len(field_names) != 1:
            return False
        field_name = field_names[0]
        definitions: list[dict[str, Any]] = []
        for datasource in datasource_snapshot:
            schema = datasource.get("metadata_schema") if isinstance(datasource, dict) else {}
            properties = schema.get("properties") if isinstance(schema, dict) else {}
            definition = properties.get(field_name) if isinstance(properties, dict) else None
            if isinstance(definition, dict):
                definitions.append(definition)
        if not definitions:
            return False
        numeric_types = {"number", "integer", "float", "double", "decimal"}
        for definition in definitions:
            field_type = str(definition.get("type") or definition.get("property_type") or "").strip().lower()
            description = str(definition.get("description") or "").lower()
            if field_type in numeric_types and "字段类型：dim" not in description:
                return False
        return True

    @classmethod
    def _attach_output_lineage(
        cls,
        outputs: list[dict[str, Any]],
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        operation_by_id = {
            str(item.get("operation_node_id")): item
            for item in operations
            if str(item.get("operation_node_id") or "")
        }
        producers: dict[str, list[str]] = {}
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                target_name = str(target or "").strip()
                if target_name:
                    producers.setdefault(target_name, []).append(operation_id)

        # Chart helpers retain the query key on the DataFrame they receive. This is
        # runtime provenance, so downstream tables and report aggregates can reuse
        # it without trying to infer a source from a field name or displayed value.
        runtime_query_sources = {
            str(output.get("source_frame") or "").strip(): str(
                output.get("source_query_node_id") or ""
            ).strip()
            for output in outputs
            if str(output.get("source_frame") or "").strip()
            and str(output.get("source_query_node_id") or "").strip()
        }
        direct_frame_query_sources: dict[str, str] = {}
        ambiguous_direct_frames: set[str] = set()
        for operation in operation_by_id.values():
            query_node_id = str(operation.get("query_node_id") or "").strip()
            if operation.get("kind") != "query_load" or not query_node_id:
                continue
            for target in operation.get("targets") or []:
                target_name = str(target or "").strip()
                if not target_name:
                    continue
                existing = direct_frame_query_sources.get(target_name)
                if existing and existing != query_node_id:
                    ambiguous_direct_frames.add(target_name)
                else:
                    direct_frame_query_sources[target_name] = query_node_id

        resolved: list[dict[str, Any]] = []
        for raw_output in outputs:
            output = dict(raw_output)
            if output.get("structured_origin"):
                resolved.append(output)
                continue
            field = str(output.get("field") or "").strip()
            declared_lineage_fields = [
                str(item or "").strip()
                for item in output.get("lineage_fields") or []
                if str(item or "").strip()
            ]
            lineage_fields = list(dict.fromkeys(
                str(item or "").strip()
                for item in (declared_lineage_fields or [field])
                if str(item or "").strip()
            ))
            direct = [
                str(item) for item in output.get("direct_operation_node_ids") or []
                if str(item) in operation_by_id
            ]
            exact_matches = [
                operation_id
                for operation_id, operation in operation_by_id.items()
                if not (
                    output.get("artifact_type") in {"chart", "table"}
                    and operation.get("kind") == "output_expression"
                )
                if lineage_fields and any(
                    cls._target_produces_field(str(item or ""), lineage_field)
                    for item in operation.get("targets") or []
                    for lineage_field in lineage_fields
                )
            ]
            source_frame = str(output.get("source_frame") or "").strip()
            source_query_node_id = str(
                output.get("source_query_node_id")
                or runtime_query_sources.get(source_frame)
                or (
                    direct_frame_query_sources.get(source_frame)
                    if source_frame not in ambiguous_direct_frames
                    else ""
                )
                or ""
            ).strip()
            if source_query_node_id:
                exact_matches = [
                    operation_id
                    for operation_id in exact_matches
                    if source_frame in (operation_by_id.get(operation_id) or {}).get("targets", [])
                    or cls._operation_reaches_query(
                        operation_id,
                        source_query_node_id,
                        operation_by_id,
                        producers,
                    )
                ]
            local_exact_matches = [
                operation_id for operation_id in exact_matches
                        if (operation_by_id.get(operation_id) or {}).get("kind") not in {"query_projection", "source_field"}
            ]
            series_value_sources = {
                str((operation_by_id.get(operation_id) or {}).get("source") or "").strip()
                for operation_id in local_exact_matches
                if (operation_by_id.get(operation_id) or {}).get("kind") == "series_value"
            }
            output["ambiguous_calculation_boundary"] = len(series_value_sources) > 1
            query_projection_matches = [
                operation_id for operation_id in exact_matches
                if (operation_by_id.get(operation_id) or {}).get("kind") in {"query_projection", "source_field"}
            ]
            metric_local_exact_matches = [
                operation_id
                for operation_id in local_exact_matches
                if (operation_by_id.get(operation_id) or {}).get("semantic_kind")
                in {"aggregation", "formula", "ranking"}
            ]
            if output.get("artifact_type") == "report" and metric_local_exact_matches:
                direct = metric_local_exact_matches
            elif not direct:
                direct.extend(local_exact_matches)
            if not direct and lineage_fields:
                mentioned = [
                    operation_id
                    for operation_id, operation in operation_by_id.items()
                    if operation.get("kind") not in {"query_projection", "source_field"}
                    if any(
                        cls._operation_mentions_field(operation, lineage_field)
                        for lineage_field in lineage_fields
                    )
                ]
                if source_query_node_id:
                    mentioned = [
                        operation_id
                        for operation_id in mentioned
                        if source_frame in (operation_by_id.get(operation_id) or {}).get("targets", [])
                        or cls._operation_reaches_query(
                            operation_id,
                            source_query_node_id,
                            operation_by_id,
                            producers,
                        )
                    ]
                metric_mentions = [
                    operation_id for operation_id in mentioned
                    if (operation_by_id.get(operation_id) or {}).get("semantic_kind")
                    in {"aggregation", "formula", "ranking"}
                ]
                if metric_mentions:
                    first_line = min(
                        int((operation_by_id.get(item) or {}).get("line") or 0)
                        for item in metric_mentions
                    )
                    direct.extend(
                        item for item in metric_mentions
                        if int((operation_by_id.get(item) or {}).get("line") or 0) == first_line
                    )
                else:
                    direct.extend(mentioned)
            if not direct and source_query_node_id:
                direct.extend(
                    operation_id
                    for operation_id, operation in operation_by_id.items()
                    if operation.get("kind") == "query_load"
                    and str(operation.get("query_node_id") or "") == source_query_node_id
                )
            if not direct:
                direct.extend(query_projection_matches)
            direct = list(dict.fromkeys(direct))

            boundaries = cls._nearest_calculation_boundaries(
                direct,
                operation_by_id,
                producers,
            )
            if not boundaries and query_projection_matches:
                direct = list(dict.fromkeys([*direct, *query_projection_matches]))
                boundaries = cls._nearest_calculation_boundaries(
                    direct,
                    operation_by_id,
                    producers,
                )
            if not boundaries:
                boundaries = cls._query_load_boundaries(
                    direct,
                    operation_by_id,
                    producers,
                )

            selected = set(direct)
            pending = list(direct)
            while pending:
                operation_id = pending.pop()
                operation = operation_by_id.get(operation_id) or {}
                for loaded_name in operation.get("loads") or []:
                    for producer_id in producers.get(str(loaded_name), []):
                        if producer_id not in selected:
                            selected.add(producer_id)
                            pending.append(producer_id)

            ordered_ids = sorted(
                selected,
                key=lambda item: (
                    int((operation_by_id.get(item) or {}).get("line") or 0),
                    item,
                ),
            )
            query_ids = cls._query_dependencies(
                boundaries or direct,
                operation_by_id,
                producers,
                runtime_query_sources,
            )
            if source_query_node_id:
                query_ids = list(dict.fromkeys([source_query_node_id, *query_ids]))
            output["direct_operation_node_ids"] = direct
            output["candidate_operation_node_ids"] = ordered_ids
            output["candidate_query_node_ids"] = query_ids
            output["boundary_operation_node_ids"] = boundaries
            resolved.append(output)
        return resolved

    @staticmethod
    def _query_load_boundaries(
        operation_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        producers: dict[str, list[str]],
    ) -> list[str]:
        pending = list(operation_ids)
        visited: set[str] = set()
        boundaries: list[str] = []
        while pending:
            operation_id = pending.pop()
            if operation_id in visited:
                continue
            visited.add(operation_id)
            operation = operation_by_id.get(operation_id) or {}
            if operation.get("semantic_kind") == "query_load" and operation.get("query_node_id"):
                boundaries.append(operation_id)
                continue
            for loaded_name in operation.get("loads") or []:
                pending.extend(producers.get(str(loaded_name), []))
        return list(dict.fromkeys(reversed(boundaries)))

    @staticmethod
    def _query_dependencies(
        operation_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        producers: dict[str, list[str]],
        runtime_query_sources: dict[str, str] | None = None,
    ) -> list[str]:
        pending = list(operation_ids)
        visited: set[str] = set()
        query_ids: list[str] = []
        while pending:
            operation_id = pending.pop()
            if operation_id in visited:
                continue
            visited.add(operation_id)
            operation = operation_by_id.get(operation_id) or {}
            query_node_id = str(operation.get("query_node_id") or "").strip()
            if query_node_id:
                query_ids.append(query_node_id)
            for loaded_name in operation.get("loads") or []:
                runtime_query_id = str(
                    (runtime_query_sources or {}).get(str(loaded_name)) or ""
                ).strip()
                if runtime_query_id:
                    query_ids.append(runtime_query_id)
                pending.extend(producers.get(str(loaded_name), []))
        return list(dict.fromkeys(reversed(query_ids)))

    @classmethod
    def _operation_reaches_query(
        cls,
        operation_id: str,
        query_node_id: str,
        operation_by_id: dict[str, dict[str, Any]],
        producers: dict[str, list[str]],
    ) -> bool:
        pending = [operation_id]
        visited: set[str] = set()
        while pending:
            current_id = pending.pop()
            if current_id in visited:
                continue
            visited.add(current_id)
            operation = operation_by_id.get(current_id) or {}
            if str(operation.get("query_node_id") or "") == query_node_id:
                return True
            for loaded_name in operation.get("loads") or []:
                pending.extend(producers.get(str(loaded_name), []))
        return False

    @staticmethod
    def _nearest_calculation_boundaries(
        direct_operation_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        producers: dict[str, list[str]],
    ) -> list[str]:
        boundary_kinds = {"aggregation", "formula", "ranking", "projection"}
        boundaries: list[str] = []
        pending = list(direct_operation_ids)
        visited: set[str] = set()
        while pending:
            operation_id = pending.pop(0)
            if operation_id in visited:
                continue
            visited.add(operation_id)
            operation = operation_by_id.get(operation_id) or {}
            if operation.get("semantic_kind") in boundary_kinds:
                boundaries.append(operation_id)
                continue
            for loaded_name in operation.get("loads") or []:
                producer_ids = producers.get(str(loaded_name), [])
                local_producers = [
                    producer_id for producer_id in producer_ids
                    if (operation_by_id.get(producer_id) or {}).get("kind") not in {"query_projection", "source_field"}
                ]
                pending.extend(local_producers or producer_ids)
        return list(dict.fromkeys(boundaries))

    @staticmethod
    def _operation_mentions_field(operation: dict[str, Any], field: str) -> bool:
        source = str(operation.get("source") or "")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        return field in {
            str(item.value)
            for item in ast.walk(tree)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }

    @staticmethod
    def _target_produces_field(target: str, field: str) -> bool:
        normalized_target = str(target or "").strip()
        if normalized_target == field:
            return True
        return normalized_target.endswith(f"[{field!r}]") or normalized_target.endswith(f'["{field}"]')

    @staticmethod
    def _locate_report_outputs(report: str, expressions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        occupied: set[tuple[int, int]] = set()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for expression in expressions:
            template_id = str(expression.get("template_id") or expression.get("expression_id") or "")
            grouped.setdefault(template_id, []).append(expression)
        for template_id, template_expressions in grouped.items():
            template_expressions.sort(key=lambda item: int(item.get("slot_index") or 0))
            tokens = template_expressions[0].get("template_tokens") or []
            if not tokens:
                continue
            pattern_parts: list[str] = []
            slot_names: list[str] = []
            for token_index, token in enumerate(tokens):
                if "literal" in token:
                    literal = str(token.get("literal") or "")
                    if token_index == 0:
                        literal = literal.lstrip()
                        pattern_parts.append(r"\s*")
                    if token_index == len(tokens) - 1:
                        literal = literal.rstrip()
                    pattern_parts.append(re.escape(literal))
                    if token_index == len(tokens) - 1:
                        pattern_parts.append(r"\s*")
                elif "slot" in token:
                    slot_name = f"slot_{int(token.get('slot') or 0)}"
                    slot_names.append(slot_name)
                    if token.get("numeric_hint"):
                        pattern_parts.append(
                            fr"(?P<{slot_name}>[-+]?(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)"
                        )
                        if token.get("numeric_suffix"):
                            pattern_parts.append(re.escape(str(token["numeric_suffix"])))
                    else:
                        pattern_parts.append(fr"(?P<{slot_name}>[^\r\n]*?)")
            if not slot_names or not any("literal" in token and str(token.get("literal") or "") for token in tokens):
                continue
            pattern = "".join(pattern_parts)
            for occurrence, match in enumerate(re.finditer(pattern, report)):
                for expression in template_expressions:
                    slot_name = f"slot_{int(expression.get('slot_index') or 0)}"
                    if slot_name not in match.groupdict():
                        continue
                    start, end = match.span(slot_name)
                    text = report[start:end]
                    if start == end or (start, end) in occupied or not _is_numeric_display_text(text):
                        continue
                    occupied.add((start, end))
                    outputs.append({
                        "output_node_id": f"out_report_{expression['expression_id']}_{occurrence}",
                        "artifact_type": "report",
                        "artifact_index": 0,
                        "field": str(expression.get("expression") or ""),
                        "label": str(expression.get("expression") or ""),
                        "lineage_fields": LineageIndexer._expression_field_names(
                            str(expression.get("expression") or "")
                        ),
                        "start": start,
                        "end": end,
                        "text": text,
                        "display_prefix": str(expression.get("prefix") or ""),
                        "display_suffix": str(expression.get("suffix") or ""),
                        "rendered_context": report[max(0, start - 120):min(len(report), end + 120)],
                        "source_line": expression.get("line"),
                        "numeric_hint": bool(expression.get("numeric_hint")),
                        "direct_operation_node_ids": [expression.get("operation_node_id")],
                    })
        return outputs

    @staticmethod
    def _expression_field_names(expression: str) -> list[str]:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            return []
        literal_fields = list(dict.fromkeys(
            str(item.value)
            for item in ast.walk(tree)
            if isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and str(item.value).strip()
        ))
        if literal_fields:
            return literal_fields
        called_names = {
            item.func.id
            for item in ast.walk(tree)
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
        }
        return list(dict.fromkeys(
            item.id
            for item in ast.walk(tree)
            if isinstance(item, ast.Name)
            and isinstance(item.ctx, ast.Load)
            and item.id not in called_names
        ))

    @staticmethod
    def _query_scope(snapshot: Any) -> dict[str, Any]:
        value = snapshot if isinstance(snapshot, dict) else {}
        implementation = value.get("implementation") if isinstance(value.get("implementation"), dict) else {}
        return {
            "query_constraints": value.get("query_constraints") or {},
            "aggregation_hints": implementation.get("aggregation_hints") or [],
            "implementation": implementation,
        }


class PostAnalysisEvidenceCompiler:
    def __init__(
        self,
        *,
        planner: Callable[[dict[str, Any]], EvidencePlan] | None = None,
    ) -> None:
        self._planner = planner or self._invoke_planner

    def compile(self, snapshot: AnalysisExecutionSnapshot) -> dict[str, Any]:
        lineage_index = LineageIndexer().build(snapshot)
        if not lineage_index["query_nodes"] or not lineage_index["output_nodes"]:
            return self._empty_bundle(snapshot, lineage_index, "insufficient_lineage")
        calculation_candidates = self._build_calculation_candidates(lineage_index)
        if not calculation_candidates:
            return self._empty_bundle(snapshot, lineage_index, "insufficient_calculation_boundaries")
        try:
            plan = self._planner({
                "user_query": snapshot.user_query,
                "datasource_context": self._relevant_datasource_context(snapshot, lineage_index),
                "calculation_candidates": self._planner_candidate_payloads(
                    calculation_candidates,
                    lineage_index,
                ),
            })
        except Exception as exc:
            logger.warning(
                "post-analysis evidence planner unavailable: execution_id=%s error=%s",
                snapshot.execution_id,
                exc,
                exc_info=True,
            )
            return self._empty_bundle(snapshot, lineage_index, "planner_unavailable", str(exc))
        reference_errors = self._plan_reference_errors(plan, calculation_candidates)
        if reference_errors:
            return self._empty_bundle(
                snapshot,
                lineage_index,
                "invalid_plan_references",
                "; ".join(reference_errors[:20]),
            )
        try:
            compiled = self._compile_plan(snapshot, lineage_index, plan, calculation_candidates)
            candidate_output_ids = {
                str(output_id)
                for candidate in calculation_candidates.values()
                for output_id in candidate.get("output_node_ids") or []
                if str(output_id)
            }
            metric_output_ids = {
                str(output.get("output_node_id") or "")
                for output in lineage_index.get("output_nodes") or []
                if str(output.get("output_node_id") or "")
                and not self._is_grouping_dimension_output(output, lineage_index)
                and self._is_report_metric_output(output, lineage_index)
                and (
                    output.get("artifact_type") in {"chart", "table"}
                    or bool(output.get("candidate_query_node_ids"))
                )
            }
            compiled_output_ids = set(compiled.get("diagnostics", {}).pop("compiled_output_node_ids", []))
            unresolved_output_ids = sorted(metric_output_ids - candidate_output_ids)
            missing_compiled_output_ids = sorted(candidate_output_ids - compiled_output_ids)
            compiled["diagnostics"].update({
                "metric_output_count": len(metric_output_ids),
                "candidate_output_count": len(candidate_output_ids),
                "compiled_output_count": len(compiled_output_ids),
                "unresolved_output_node_ids": unresolved_output_ids,
                "missing_compiled_output_node_ids": missing_compiled_output_ids,
            })
            compiled["status"] = (
                "complete"
                if compiled["evidence_definitions"]
                and not unresolved_output_ids
                and not missing_compiled_output_ids
                else "partial"
            )
            return compiled
        except Exception as exc:
            logger.warning(
                "post-analysis evidence compilation failed: execution_id=%s error=%s",
                snapshot.execution_id,
                exc,
                exc_info=True,
            )
            return self._empty_bundle(snapshot, lineage_index, "compile_failed", str(exc))

    @staticmethod
    def _is_grouping_dimension_output(
        output: dict[str, Any],
        lineage_index: dict[str, Any],
    ) -> bool:
        boundary_ids = {
            str(item)
            for item in output.get("boundary_operation_node_ids") or []
            if str(item)
        }
        if not boundary_ids:
            return False
        operations = [
            operation
            for operation in lineage_index.get("operation_nodes") or []
            if str(operation.get("operation_node_id") or "") in boundary_ids
        ]
        return bool(operations) and all(
                operation.get("kind") in {"query_projection", "source_field"}
            and operation.get("projection_role") == "dimension"
            for operation in operations
        )

    @staticmethod
    def _is_report_metric_output(
        output: dict[str, Any],
        lineage_index: dict[str, Any],
    ) -> bool:
        if output.get("artifact_type") != "report" or output.get("numeric_hint"):
            return True
        boundary_ids = {
            str(item)
            for item in output.get("boundary_operation_node_ids") or []
            if str(item)
        }
        operations = [
            operation
            for operation in lineage_index.get("operation_nodes") or []
            if str(operation.get("operation_node_id") or "") in boundary_ids
        ]
        return any(
            operation.get("semantic_kind") in {"aggregation", "formula", "ranking"}
            or (
                operation.get("kind") in {"query_projection", "source_field"}
                and operation.get("projection_role") == "metric"
            )
            for operation in operations
        )

    @staticmethod
    def _build_calculation_candidates(lineage_index: dict[str, Any]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for output in lineage_index.get("output_nodes") or []:
            if output.get("ambiguous_calculation_boundary"):
                continue
            if not PostAnalysisEvidenceCompiler._is_report_metric_output(
                output,
                lineage_index,
            ):
                continue
            boundaries = list(dict.fromkeys(
                str(item) for item in output.get("boundary_operation_node_ids") or [] if str(item)
            ))
            query_ids = list(dict.fromkeys(
                str(item) for item in output.get("candidate_query_node_ids") or [] if str(item)
            ))
            operation_ids = list(dict.fromkeys(
                str(item) for item in output.get("candidate_operation_node_ids") or [] if str(item)
            ))
            if not boundaries or not query_ids or not operation_ids:
                continue
            if PostAnalysisEvidenceCompiler._is_grouping_dimension_output(output, lineage_index):
                continue
            boundary = next(
                (
                    item for item in lineage_index.get("operation_nodes") or []
                    if str(item.get("operation_node_id") or "") == boundaries[0]
                ),
                {},
            )
            identity_payload: dict[str, Any] = {
                "boundaries": sorted(boundaries),
                "queries": sorted(query_ids),
                "field": str(output.get("field") or ""),
            }
            identity = _stable_json(identity_payload)
            calculation_id = f"calc_{_digest(identity, 20)}"
            candidate = grouped.setdefault(calculation_id, {
                "calculation_id": calculation_id,
                "boundary_operation_node_id": boundaries[0],
                "boundary_operation_node_ids": boundaries,
                "output_node_ids": [],
                "operation_node_ids": [],
                "query_node_ids": query_ids,
            })
            candidate["output_node_ids"].append(str(output.get("output_node_id") or ""))
            candidate["operation_node_ids"] = list(dict.fromkeys([
                *candidate["operation_node_ids"],
                *operation_ids,
            ]))
        return grouped

    @staticmethod
    def _planner_candidate_payloads(
        candidates: dict[str, dict[str, Any]],
        lineage_index: dict[str, Any],
    ) -> list[dict[str, Any]]:
        output_by_id = {
            str(item.get("output_node_id")): item for item in lineage_index.get("output_nodes") or []
        }
        operation_by_id = {
            str(item.get("operation_node_id")): item for item in lineage_index.get("operation_nodes") or []
        }
        query_by_id = {
            str(item.get("query_node_id")): item for item in lineage_index.get("query_nodes") or []
        }
        payloads: list[dict[str, Any]] = []
        for calculation_id, candidate in candidates.items():
            boundary = operation_by_id.get(str(candidate.get("boundary_operation_node_id"))) or {}
            boundary_operations = [
                operation_by_id.get(str(operation_id)) or {}
                for operation_id in candidate.get("boundary_operation_node_ids") or []
            ]
            payloads.append({
                "calculation_id": calculation_id,
                "calculation_kind": boundary.get("semantic_kind") or "",
                "calculation_expressions": list(dict.fromkeys(
                    str(operation.get("source") or "").strip()
                    for operation in boundary_operations
                    if str(operation.get("source") or "").strip()
                )),
                "outputs": [
                    {
                        "artifact_type": output.get("artifact_type"),
                        "label": output.get("label"),
                        "field": output.get("field"),
                        "sample_values": output.get("sample_values") or [],
                        "display_context": {
                            "prefix": output.get("display_prefix") or "",
                            "suffix": output.get("display_suffix") or "",
                            "rendered_context": output.get("rendered_context") or "",
                        } if output.get("artifact_type") == "report" else {},
                    }
                    for output_id in candidate.get("output_node_ids") or []
                    for output in [output_by_id.get(str(output_id)) or {}]
                ],
                "query_scopes": [
                    (query_by_id.get(str(query_id)) or {}).get("query_scope") or {}
                    for query_id in candidate.get("query_node_ids") or []
                ],
            })
        return payloads

    @staticmethod
    def _relevant_datasource_context(
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
    ) -> list[dict[str, Any]]:
        search_text = "\n".join(
            [
                str(item.get("field") or "")
                for item in lineage_index.get("output_nodes") or []
            ]
            + [
                str(item.get("source") or "")
                for item in lineage_index.get("operation_nodes") or []
            ]
        )
        context: list[dict[str, Any]] = []
        for datasource in snapshot.selected_datasource_snapshot:
            if not isinstance(datasource, dict):
                continue
            schema = datasource.get("metadata_schema") if isinstance(datasource.get("metadata_schema"), dict) else {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            relevant_properties = {
                str(field_name): definition
                for field_name, definition in properties.items()
                if str(field_name) and str(field_name) in search_text
            }
            context.append({
                "datasource_id": datasource.get("datasource_id") or datasource.get("id"),
                "datasource_name": datasource.get("datasource_name") or "",
                "datasource_identifier": datasource.get("datasource_identifier") or "",
                "relevant_properties": relevant_properties,
            })
        return context

    def _invoke_planner(self, payload: dict[str, Any]) -> EvidencePlan:
        if not bool(getattr(Config, "POST_ANALYSIS_EVIDENCE_PLANNER_ENABLED", True)):
            raise RuntimeError("Evidence Planner is disabled")
        runtime_config = create_runtime_config()
        model = create_data_insight_model(runtime_config)
        messages: list[Any] = [
            SystemMessage(EVIDENCE_PLANNER_PROMPT),
            HumanMessage(_stable_json(payload)),
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            response_text = ""
            try:
                response = model.invoke(messages)
                content = getattr(response, "content", response)
                if isinstance(content, list):
                    response_text = "".join(
                        str(item.get("text") or "") if isinstance(item, dict) else str(item)
                        for item in content
                    )
                else:
                    response_text = str(content or "")
                plan = EvidencePlan.model_validate(_extract_json_object(response_text))
                candidates = {
                    str(item.get("calculation_id")): item
                    for item in payload.get("calculation_candidates") or []
                    if str(item.get("calculation_id") or "")
                }
                reference_errors = self._plan_reference_errors(plan, candidates)
                if reference_errors:
                    raise ValueError("; ".join(reference_errors[:20]))
                return plan
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 0:
                    if response_text:
                        messages.append(AIMessage(response_text))
                    messages.append(HumanMessage(
                        "上一输出不符合 JSON 或候选标注契约。请为每个 calculation_id 恰好输出一次，只修正结构："
                        + str(exc)[:1200]
                    ))
                    continue
                raise
        raise RuntimeError(f"Evidence Planner failed: {last_error}")

    @staticmethod
    def _plan_reference_errors(
        plan: EvidencePlan,
        calculation_candidates: dict[str, dict[str, Any]],
    ) -> list[str]:
        expected_ids = set(calculation_candidates)
        errors: list[str] = []
        claimed_ids: set[str] = set()
        for metric_index, metric in enumerate(plan.metrics):
            calculation_id = str(metric.calculation_id or "").strip()
            if calculation_id not in expected_ids:
                errors.append(f"metrics[{metric_index}].calculation_id is unknown: {calculation_id}")
                continue
            if calculation_id in claimed_ids:
                errors.append(f"calculation_id is emitted more than once: {calculation_id}")
            claimed_ids.add(calculation_id)
        missing_ids = expected_ids - claimed_ids
        if missing_ids:
            errors.append(f"missing calculation candidates: {sorted(missing_ids)}")
        return errors

    @staticmethod
    def _expand_operation_ids(
        operation_ids: list[str],
        query_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        selected = set(operation_ids)
        for operation_id, operation in operation_by_id.items():
            if operation.get("kind") == "validated_query" and str(operation.get("query_node_id")) in query_ids:
                selected.add(operation_id)
        return sorted(
            selected,
            key=lambda item: (
                int((operation_by_id.get(item) or {}).get("line") or 0),
                item,
            ),
        )

    def _compile_plan(
        self,
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        plan: EvidencePlan,
        calculation_candidates: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        output_by_id = {str(item["output_node_id"]): item for item in lineage_index["output_nodes"]}
        operation_by_id = {str(item["operation_node_id"]): item for item in lineage_index["operation_nodes"]}
        query_by_id = {str(item["query_node_id"]): item for item in lineage_index["query_nodes"]}
        definitions_by_signature: dict[str, dict[str, Any]] = {}
        output_claims: dict[str, str] = {}
        chart_refs: list[dict[str, Any]] = []
        table_refs: list[dict[str, Any]] = []
        report_refs: list[dict[str, Any]] = []

        for metric in plan.metrics:
            candidate = calculation_candidates.get(str(metric.calculation_id or "")) or {}
            output_ids = list(dict.fromkeys(candidate.get("output_node_ids") or []))
            selected_outputs = [output_by_id[item] for item in output_ids if item in output_by_id]
            candidate_operation_ids = list(dict.fromkeys(
                str(operation_id)
                for operation_id in candidate.get("operation_node_ids") or []
                if str(operation_id) in operation_by_id
            ))
            query_ids = list(dict.fromkeys(
                str(query_id)
                for query_id in candidate.get("query_node_ids") or []
                if str(query_id) in query_by_id
            ))
            operation_ids = self._expand_operation_ids(
                candidate_operation_ids,
                query_ids,
                operation_by_id,
            )
            if not output_ids or not operation_ids or not query_ids:
                continue
            if any(item not in output_by_id for item in output_ids):
                continue
            if any(item not in operation_by_id for item in operation_ids):
                continue
            if any(item not in query_by_id for item in query_ids):
                continue
            signature_payload = {
                "queries": sorted(query_ids),
                "operations": [operation_by_id[item]["source"] for item in sorted(operation_ids)],
                "metric_fields": sorted(
                    str(output_by_id[item].get("field") or "").strip()
                    for item in output_ids
                    if str(output_by_id[item].get("field") or "").strip()
                ),
            }
            signature = _digest(_stable_json(signature_payload), 24)
            if any(output_claims.get(item) not in (None, signature) for item in output_ids):
                continue
            evidence_key = f"calc_{signature}"
            for output_id in output_ids:
                output_claims[output_id] = signature
            metric_fields = list(dict.fromkeys(
                str(output_by_id[item].get("field") or "").strip()
                for item in output_ids
                if str(output_by_id[item].get("field") or "").strip()
            ))
            if signature not in definitions_by_signature:
                boundary = operation_by_id.get(str(candidate.get("boundary_operation_node_id"))) or {}
                definitions_by_signature[signature] = {
                    "evidence_key": evidence_key,
                    "metric_name": metric.business_name.strip() or metric.metric_key,
                    "value_type": metric.value_type,
                    "unit": metric.display_unit,
                    "query_refs": query_ids,
                    "derivation": {
                        "mode": self._derivation_mode(boundary.get("semantic_kind")),
                        "description": metric.business_explanation.strip(),
                    },
                    "calculation": {
                        "metric_fields": metric_fields,
                        "calculation_signature": signature,
                        "operation_node_ids": operation_ids,
                    },
                    "transformations": list(dict.fromkeys(
                        self._describe_operation(operation_by_id[item])
                        for item in operation_ids
                        if (operation_by_id[item].get("semantic_kind") != "presentation")
                    )),
                    "intermediate_outputs": [],
                    "quality_notes": [],
                }
            else:
                existing_fields = definitions_by_signature[signature]["calculation"]["metric_fields"]
                definitions_by_signature[signature]["calculation"]["metric_fields"] = list(dict.fromkeys(
                    [*existing_fields, *metric_fields]
                ))
                if not definitions_by_signature[signature].get("unit") and metric.display_unit:
                    definitions_by_signature[signature]["unit"] = metric.display_unit
                if not definitions_by_signature[signature].get("value_type") and metric.value_type:
                    definitions_by_signature[signature]["value_type"] = metric.value_type

            for output_id in output_ids:
                output = output_by_id[output_id]
                artifact_type = output.get("artifact_type")
                if artifact_type == "chart":
                    chart_refs.append({
                        "chart_index": int(output.get("artifact_index") or 0),
                        "artifact_id": int(output.get("artifact_id") or 0),
                        "value_field": output.get("field"),
                        "evidence_key": evidence_key,
                        "display_unit": metric.display_unit,
                    })
                elif artifact_type == "table":
                    table_refs.append({
                        "table_index": int(output.get("artifact_index") or 0),
                        "artifact_id": int(output.get("artifact_id") or 0),
                        "column_key": output.get("field"),
                        "row_scope": "all",
                        "evidence_key": evidence_key,
                        "display_unit": metric.display_unit,
                    })
                elif artifact_type == "report":
                    start = int(output.get("start") or 0)
                    end = int(output.get("end") or 0)
                    text = str(output.get("text") or "")
                    if start < end and snapshot.analysis_report[start:end] == text:
                        report_refs.append({
                            "report_index": 0,
                            "artifact_id": int(output.get("artifact_id") or 0),
                            "start": start,
                            "end": end,
                            "text": text,
                            "evidence_key": evidence_key,
                            "display_unit": metric.display_unit,
                        })

        return {
            "evidence_definitions": list(definitions_by_signature.values()),
            "chart_trace_refs": self._dedupe_refs(chart_refs, ("chart_index", "value_field")),
            "table_trace_refs": self._dedupe_refs(table_refs, ("table_index", "column_key")),
            "report_trace_refs": self._dedupe_refs(report_refs, ("start", "end")),
            "query_snapshots": snapshot.query_snapshots,
            "diagnostics": {
                "code_hash": snapshot.code_hash,
                "result_hash": snapshot.result_hash,
                "lineage_operation_count": len(lineage_index["operation_nodes"]),
                "lineage_output_count": len(lineage_index["output_nodes"]),
                "compiled_output_node_ids": sorted(output_claims),
            },
        }

    @staticmethod
    def _derivation_mode(semantic_kind: Any) -> str:
        kind = str(semantic_kind or "").strip()
        if kind == "formula":
            return "formula"
        if kind == "ranking":
            return "algorithm"
        return "query_output"

    @staticmethod
    def _describe_operation(operation: dict[str, Any]) -> str:
        descriptions = {
            "query": "按本轮确认的数据范围读取记录。",
            "query_load": "读取已确认查询返回的数据。",
            "projection": "读取查询结果中的目标指标列。",
            "grouping": "按业务维度整理数据。",
            "aggregation": "对目标范围内的数据进行汇总统计。",
            "formula": "根据基础统计结果计算派生指标。",
            "ranking": "按指标结果排序并定位相应对象。",
            "join": "按已确认的业务关联关系合并相关数据。",
            "transformation": "完成必要的数据清洗和字段转换。",
        }
        return descriptions.get(
            str(operation.get("semantic_kind") or "").strip(),
            "根据本轮查询结果完成指标计算。",
        )

    @staticmethod
    def _dedupe_refs(refs: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for ref in refs:
            signature = tuple(ref.get(key) for key in keys)
            if signature in seen:
                continue
            seen.add(signature)
            result.append(ref)
        return result

    @staticmethod
    def _empty_bundle(
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        reason: str,
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "status": "partial" if lineage_index.get("output_nodes") else "failed",
            "evidence_definitions": [],
            "chart_trace_refs": [],
            "table_trace_refs": [],
            "report_trace_refs": [],
            "query_snapshots": snapshot.query_snapshots,
            "diagnostics": {
                "reason": reason,
                "error": error[:1200],
                "code_hash": snapshot.code_hash,
                "result_hash": snapshot.result_hash,
            },
        }


def build_execution_snapshot(
    *,
    execution: Any,
    artifacts: list[dict[str, Any]],
    selected_datasource_snapshot: list[dict[str, Any]],
    user_query: str,
) -> AnalysisExecutionSnapshot:
    payload = safe_json_loads(getattr(execution, "result_payload_json", "") or "{}", {})
    payload = payload if isinstance(payload, dict) else {}
    charts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    report = str(getattr(execution, "analysis_report", "") or "")
    report_artifact_id = 0
    ordered_artifacts = sorted(
        artifacts,
        key=lambda item: (
            str(item.get("artifact_type") or ""),
            int(item.get("sort_no") or 0),
            int(item.get("id") or 0),
        ),
    )
    for artifact in ordered_artifacts:
        content = _artifact_content(artifact)
        content = dict(content)
        content["_artifact_id"] = int(artifact.get("id") or 0)
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type == "chart":
            charts.append(content)
        elif artifact_type == "table":
            tables.append(content)
        elif artifact_type == "report":
            report = str(content.get("report_markdown") or report)
            report_artifact_id = int(artifact.get("id") or 0)
    generated_code = str(getattr(execution, "generated_code", "") or "")
    result_material = {
        "analysis_report": report,
        "charts": charts,
        "tables": tables,
        "query_snapshots": payload.get("query_snapshots") or {},
    }
    return AnalysisExecutionSnapshot(
        execution_id=int(getattr(execution, "id", 0) or 0),
        code_hash=_digest(generated_code, 32),
        result_hash=_digest(_stable_json(result_material), 32),
        generated_code=generated_code,
        query_snapshots=payload.get("query_snapshots") if isinstance(payload.get("query_snapshots"), dict) else {},
        selected_datasource_snapshot=selected_datasource_snapshot,
        user_query=str(user_query or ""),
        analysis_report=report,
        charts=charts,
        tables=tables,
        report_artifact_id=report_artifact_id,
    )
