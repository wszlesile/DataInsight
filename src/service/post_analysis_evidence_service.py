from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import re
import string
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError

from config import Config
from utils import logger
from utils.datasource_utils import safe_json_loads
from utils.llm_model_factory import create_data_insight_model, create_runtime_config


EVIDENCE_PLANNER_PROMPT = """
你是数据洞察报告的证据关系补漏专家。上游分析已经成功完成，查询、计算和报告均不可修改。图表和表格以及大部分报告数值已经由本地完成；当前只处理输入目录中仍未解决的报告位置。

analysis_code_with_lines 是带行号的完整成功 Python 源码，是定位计算边界的主要事实。report_candidate_catalog 与 source_anchor_catalog 是本次唯一待分类范围。阅读完整代码只是为了理解这些位置如何计算，不要重新枚举目录以外、已经由本地处理过的报告表达式，也不要读取或猜测最终 Markdown 数值。

source_anchor_catalog 是代码中动态字符串表达式的纯语法索引，包含报告、图表描述和其他字符串；它不是报告指标候选。你可以引用其中的 anchor_id，也可以在目录漏掉合法表达式时提交准确的 source_line 与 expression。operation_catalog 是可验证的代码语句目录，不是候选清单。query_catalog 只描述本轮成功数据访问范围；最终查询关系由本地根据所选 operation 的依赖闭包确定。

规则：
1. 每个 metric 必须引用输入中的 source_anchors 或 report_candidate_ids。目录已有 anchor 时使用 anchor_id；渲染候选使用 report_candidate_ids，并通过 operation_node_ids 或 calculation_source_refs 声明其最小计算边界。不得提交目录之外的表达式作为待分类位置。
2. operation_node_ids 只选择真实参与该指标的最小计算语句集合。不要为了“看起来完整”选择整个分析流程。
3. 同一源码表达式只能属于一个 metric；同一计算边界的重复报告表达可以归入同一个 metric。不同查询、字段、公式或参数绑定不得仅因名称相似而合并。
4. 单条明细值也是数据事实，不能仅因它不是汇总指标而放入 unresolved_outputs。代码通过 idxmax/idxmin、sort/head、rank 等先选择目标行再读取该行指标时，排名选择和被选指标共同构成一个复合计算。
5. 输入目录中的每一项必须且只能归入 metric、non_metric 对应字段或 unresolved_outputs 之一；不要处理目录以外的源码表达式。
6. 年份、编号、排名序号、展示标签等不属于数据计算结果的报告表达式放入 non_metric_source_anchors；代码关系无法确认时放入 unresolved_outputs，不得猜测。
7. 不要输出 SQL、Python 代码、字符区间、Evidence ID、最终数值或 metric_source_fields。
8. business_name 说明业务指标含义，business_explanation 简洁说明统计对象、范围和计算口径。
9. value_type 只能是 number、integer、currency、percentage、duration、other。display_unit 使用面向用户的单位。
10. 面向用户的文字不得出现 Runtime、Metric、Evidence、AST、API 等内部术语，也不得复述 SQL、Python、计算结果或 Markdown。

只输出一个 JSON 对象：
{
  "metrics": [{
    "calculation_id": "report_semantic_scope",
    "source_anchors": [{"anchor_id": "a1", "source_line": 0, "expression": ""}],
    "operation_node_ids": ["operation_..."],
    "calculation_source_refs": [{"source_line": 120, "expression": "len(active_rows)"}],
    "claimed_query_node_ids": ["q1"],
    "claimed_metric_fields": ["status"],
    "attestation_confidence": "high",
    "parameter_bindings": {"loop_variable": "literal value when one source slot is reused"},
    "metric_key": "稳定英文语义名",
    "business_name": "业务指标名称",
    "business_explanation": "面向用户的统计口径说明",
    "value_type": "number|integer|currency|percentage|duration|other",
    "display_unit": "元/%/台/个/小时等，没有则为空"
  }],
  "unresolved_outputs": [{
    "source_anchor": {"anchor_id": "a2", "source_line": 0, "expression": ""},
    "reason": "无法证明绑定关系的原因"
  }],
  "non_metric_source_anchors": [{"anchor_id": "a3", "source_line": 0, "expression": ""}]
}
""".strip() + """

Additional posthoc report-candidate rules:
11. report_candidate_catalog contains every rendered numeric span not already
   located by an AST report template. It is a high-recall candidate set, not a
   metric list. Classify every candidate exactly once: include its id in a
   metric.report_candidate_ids when it is a data result, put it in
   non_metric_report_candidate_ids for dates/ordinals/static labels, or add an
   unresolved_outputs item with output_node_id when no executed calculation can
   be proven.
12. A metric may use source_anchors, report_candidate_ids, or both. For a
    report candidate select the smallest operation set that produced the shown
    business value. Do not invent an operation merely because its value could
    look similar.
13. operation_node_ids may reference catalogued operations. When the exact
    calculation is visible in analysis_code_with_lines but absent from
    operation_catalog, use calculation_source_refs with the exact source_line
    and the smallest complete Python expression or assignment. This is a
    locator claim only: local AST and QuerySnapshot verification decides whether
    it can become Evidence. Do not use a surrounding report-format expression
    when a distinct calculation expression exists.
14. When calculation_selection_mode is source_locator, operation_catalog may
    contain only branch-disambiguation operations for business-labelled mapping
    members. Use such an operation when its literal label matches the rendered
    candidate; otherwise use calculation_source_refs rather than inventing an
    operation ID.
15. source_line_hints are local text-retrieval hints for a rendered candidate,
    not calculation proof. Inspect those lines and nearby variable definitions,
    then return the exact calculation expression that produced that candidate.
16. For every source-locator metric also return claimed_query_node_ids using
    ids from query_catalog, claimed_metric_fields using real projected fields,
    and attestation_confidence. Use high only when the candidate, source
    expression, query and field boundary are all certain. Local verification may
    accept a high-confidence claim when dynamic Python dataflow is beyond local
    capability, but it will reject nonexistent or contradictory references.
17. text_examples identifies the exact unresolved numeric span, not every number
    in the surrounding line. Other numbers in that sentence may already be
    locally resolved. Bind only the expression producing text_examples. For a
    joined loop or repeated template, use the yielded value expression and put
    the dimension value that selects this occurrence in parameter_bindings;
    do not bind the whole paragraph or summary object.
18. A catalog candidate is not presumed to be a metric. First decide whether
    it is a rendered data fact, a presentation literal, or genuinely unresolved.
    Date/time scope fragments (including abbreviated end dates), condition or
    comparison cutoffs (with or without a unit), ranking/section ordinals, and
    display limits are non-metric even when they appear beside a query-derived
    expression. Only a value that states an observed or calculated business
    result may be included in metric.report_candidate_ids.
19. A candidate with runtime_iteration_boundary_required=true is a numeric
    expression rendered once for each independently queried runtime member.
    Candidates under different headings are different calculation boundaries
    even when they use the same source expression and display the same value.
    Never merge those headings into one metric.

The JSON object may include report_candidate_ids and calculation_source_refs on
each metric and a top-level non_metric_report_candidate_ids array.
"""


STRUCTURED_EVIDENCE_RESOLVER_PROMPT = """
You recover calculation lineage only for chart/table numeric output slots that
the deterministic compiler could not prove. The successful Python analysis and
its outputs must not be changed. Inspect only structured_output_catalog.

For every output_node_id, either bind it to exactly one metric or return it in
unresolved_outputs. Metrics that share the same parameterized calculation may
contain multiple output_node_ids. Locate the smallest complete Python
calculation with calculation_source_refs. Use only query ids and real fields
from query_catalog. attestation_confidence may be high only when the output,
calculation, query, and field boundary are all certain. Do not return values,
SQL, evidence ids, Markdown, or outputs outside the catalog.

Return one JSON object matching this shape:
{
  "metrics": [{
    "calculation_id": "structured_recovery",
    "output_node_ids": ["out_..."],
    "calculation_source_refs": [{"source_line": 1, "expression": "..."}],
    "claimed_query_node_ids": ["q_..."],
    "claimed_metric_fields": ["field"],
    "attestation_confidence": "high|medium|low",
    "metric_key": "stable_semantic_name",
    "business_name": "user-facing metric name",
    "business_explanation": "user-facing scope and calculation explanation",
    "value_type": "number|integer|currency|percentage|duration|other",
    "display_unit": ""
  }],
  "unresolved_outputs": [{"output_node_id": "out_...", "reason": "..."}]
}
""".strip()


class ReportSourceAnchorRef(BaseModel):
    anchor_id: str = ""
    source_line: int = 0
    expression: str = ""


class CalculationSourceRef(BaseModel):
    """Planner-located calculation syntax that still requires local proof."""

    source_line: int
    expression: str


class PlannedMetric(BaseModel):
    calculation_id: str
    source_anchors: list[ReportSourceAnchorRef] = Field(default_factory=list)
    # Posthoc candidates are spans discovered from the rendered report rather
    # than from an AST render template. They are only accepted after an LLM
    # claim is reconciled with an executed calculation/query path.
    report_candidate_ids: list[str] = Field(default_factory=list)
    output_node_ids: list[str] = Field(default_factory=list)
    operation_node_ids: list[str] = Field(default_factory=list)
    calculation_source_refs: list[CalculationSourceRef] = Field(default_factory=list)
    claimed_query_node_ids: list[str] = Field(default_factory=list)
    claimed_metric_fields: list[str] = Field(default_factory=list)
    attestation_confidence: Literal["high", "medium", "low"] = "medium"
    lineage_verification: Literal["verified", "attested"] = "verified"
    query_node_ids: list[str] = Field(default_factory=list)
    metric_source_fields: list[str] = Field(default_factory=list)
    parameter_bindings: dict[str, Any] = Field(default_factory=dict)
    metric_key: str
    business_name: str
    business_explanation: str
    value_type: Literal["number", "integer", "currency", "percentage", "duration", "other"] = "number"
    display_unit: str = ""


class UnresolvedEvidenceOutput(BaseModel):
    output_node_id: str = ""
    source_anchor: ReportSourceAnchorRef | None = None
    reason: str


class EvidencePlan(BaseModel):
    metrics: list[PlannedMetric] = Field(default_factory=list)
    unresolved_outputs: list[UnresolvedEvidenceOutput] = Field(default_factory=list)
    non_metric_source_anchors: list[ReportSourceAnchorRef] = Field(default_factory=list)
    non_metric_report_candidate_ids: list[str] = Field(default_factory=list)


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


def _producer_keys(target: Any) -> list[str]:
    """Return exact and container-level keys for one operation target.

    Writing ``records['amount']`` mutates ``records``. A later load of the
    whole container therefore depends on that member write even when Python's
    syntax no longer mentions the field name (for example while constructing a
    DataFrame from the record list).
    """
    target_name = str(target or "").strip()
    if not target_name:
        return []
    keys = [target_name]
    try:
        expression = ast.parse(target_name, mode="eval").body
    except SyntaxError:
        expression = None
    if isinstance(expression, ast.Subscript):
        root = expression.value
        while isinstance(root, ast.Subscript):
            root = root.value
        if isinstance(root, ast.Name):
            keys.append(root.id)
    return list(dict.fromkeys(keys))


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


def _is_compound_numeric_display_text(value: Any, label: Any) -> bool:
    label_text = str(label or "").strip().lower()
    if not re.search(r"分子|分母|数量|比率|numerator|denominator|count|ratio", label_text):
        return False
    parts = [part.strip() for part in str(value or "").replace(",", "").split("/")]
    return len(parts) == 2 and all(
        part and _is_numeric_display_text(part)
        for part in parts
    )


def _is_markdown_ordinal_slot(prefix: Any, suffix: Any) -> bool:
    """Return whether a dynamic string slot only renders a list ordinal."""
    line_prefix = str(prefix or "").rsplit("\n", 1)[-1]
    line_suffix = str(suffix or "").split("\n", 1)[0]
    return bool(
        re.fullmatch(
            r"\s*(?:(?:#{1,6}|[-+*])\s+)?(?:\*\*)?",
            line_prefix,
        )
        and re.match(r"(?:[.)、]|）)(?=\s|\*\*)", line_suffix)
    )


_STRUCTURED_NUMERIC_DISPLAY_PATTERN = re.compile(
    r"^\s*(?P<prefix>[￥¥$€£])?\s*"
    r"(?P<open>\()?\s*(?P<number>[+-]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<close>\))?\s*(?P<suffix>[%‰\u4e00-\u9fff/·]+)?\s*$"
)


def _structured_numeric_display(values: list[Any]) -> dict[str, Any] | None:
    """Describe a numeric table column without interpreting arbitrary prose.

    Table cells are already structured values. A text column is numeric only when
    every populated cell follows the same scalar display grammar and uses the same
    prefix/suffix. Dates and free text therefore never become metric slots.
    """
    populated = [value for value in values if value is not None and str(value).strip()]
    if not populated:
        return None
    if all(_is_numeric_value(value) for value in populated):
        if not all(math.isfinite(float(value)) for value in populated):
            return None
        return {"display_unit": "", "numeric_values": populated}

    parsed: list[tuple[Decimal, str, str]] = []
    for value in populated:
        if not isinstance(value, str):
            return None
        match = _STRUCTURED_NUMERIC_DISPLAY_PATTERN.fullmatch(value)
        if match is None or bool(match.group("open")) != bool(match.group("close")):
            return None
        try:
            number = Decimal(match.group("number").replace(",", ""))
        except InvalidOperation:
            return None
        if not number.is_finite():
            return None
        if match.group("open"):
            number = -abs(number)
        parsed.append((number, match.group("prefix") or "", match.group("suffix") or ""))

    affixes = {(prefix, suffix) for _, prefix, suffix in parsed}
    if len(affixes) != 1:
        return None
    prefix, suffix = next(iter(affixes))
    if not prefix and not suffix:
        return None
    return {
        "display_unit": f"{prefix}{suffix}",
        "numeric_values": [float(number) for number, _, _ in parsed],
    }


class LineageIndexer:
    """Build a conservative code-to-artifact index without inferring business semantics."""

    _OPERATION_CALLS = {
        "groupby", "agg", "aggregate", "sum", "mean", "count", "nunique", "min", "max",
        "value_counts", "sort_values", "head", "tail", "rank", "pct_change", "rolling",
        "resample", "cumsum", "cumcount", "quantile", "describe", "merge", "join", "melt", "pivot", "pivot_table",
        "len", "size", "idxmin", "idxmax", "to_numeric", "astype", "fillna", "dropna",
        "replace", "rename", "drop_duplicates", "copy",
        "sorted", "dataframe",
    }

    @staticmethod
    def _explicit_display_unit(*values: str) -> str:
        """Return only units explicitly encoded by artifact presentation metadata."""
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if re.fullmatch(r"[%‰￥¥$€£]", text):
                return text
            match = re.match(
                r"^(?P<unit>[%‰￥¥$€£]|百分点|[\u4e00-\u9fff](?:/[\u4e00-\u9fff]{1,4})?)"
                r"(?=\s|\*\*|[，。；、,.;：:）)]|$)",
                text,
            )
            if match:
                return match.group("unit")
            match = re.search(r"[（(]([^（）()]{1,12})[）)]\s*$", text)
            if match:
                candidate = match.group(1).strip()
                if re.fullmatch(
                    r"(?:[%‰￥¥$€£]|百分点|元|万元|亿元|人民币|美元|"
                    r"个|条|次|台|套|件|笔|份|名|项|组|批|张|人|户|家|"
                    r"秒|分钟|小时|天|日|周|月|年|克|千克|公斤|吨|米|"
                    r"平方米|立方米|千瓦|千瓦时|摄氏度|"
                    r"[A-Za-z][A-Za-z0-9%‰/._-]{0,11})",
                    candidate,
                ):
                    return candidate
        return ""

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
        outputs = self._attach_runtime_output_query_identity(outputs, snapshot)
        outputs = self._attach_structured_output_origins(
            outputs,
            code=snapshot.generated_code,
            query_snapshots=query_snapshots,
            operations=operations,
        )
        outputs = self._attach_report_markdown_table_origins(
            outputs,
            code=snapshot.generated_code,
        )
        outputs = self._attach_output_lineage(
            outputs,
            operations,
            code=snapshot.generated_code,
        )
        outputs = self._mark_mapping_metric_mixtures(
            outputs,
            code=snapshot.generated_code,
        )
        heterogeneous_rankings = self._heterogeneous_ranking_issues(
            snapshot.generated_code
        )
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
            "report_source_anchors": self._source_expression_catalog(
                snapshot.generated_code
            ),
            "literal_iteration_bindings": self._literal_iteration_bindings(
                snapshot.generated_code
            ),
            "report_construction_issues": self._report_construction_issues(
                snapshot.generated_code
            ),
            "heterogeneous_rankings": heterogeneous_rankings,
        }

    @staticmethod
    def _mark_ranking_metric_mixtures(
        outputs: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project invalid ranking boundaries onto matching structured fields."""
        invalid_fields = {
            (
                str(issue.get("result_frame") or "").strip(),
                str(issue.get("value_field") or "").strip(),
            )
            for issue in issues
            if str(issue.get("result_frame") or "").strip()
            and str(issue.get("value_field") or "").strip()
        }
        if not invalid_fields:
            return outputs
        enriched: list[dict[str, Any]] = []
        for raw_output in outputs:
            output = dict(raw_output)
            if (
                output.get("artifact_type") in {"chart", "table"}
                and (
                    str(output.get("source_frame") or "").strip(),
                    str(output.get("field") or "").strip(),
                ) in invalid_fields
            ):
                output["ambiguous_calculation_boundary"] = True
                output["heterogeneous_structured_value"] = True
            enriched.append(output)
        return enriched

    @classmethod
    def _heterogeneous_ranking_issues(cls, code: str) -> list[dict[str, Any]]:
        """Find rankings whose members do not share one comparable measure.

        This is source-dataflow analysis, not report-text interpretation. It
        follows scalar aliases back to DataFrame fields and only inspects a
        container when the generated program actually orders that container by
        its numeric member. Category partitions over one field remain valid;
        rates with one explicit shared denominator remain comparable.
        """
        try:
            tree = ast.parse(str(code or ""))
        except SyntaxError:
            return []

        assignments: dict[str, list[tuple[int, ast.AST]]] = {}
        field_assignments: dict[tuple[str, str], list[tuple[int, ast.AST]]] = {}
        container_values: dict[str, ast.AST] = {}
        container_appends: dict[str, list[tuple[int, ast.AST]]] = {}
        function_definitions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }

        def slice_text(node: ast.AST) -> str:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value.strip()
            return ""

        loaded_lines: dict[str, list[int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loaded_lines.setdefault(node.id, []).append(
                    int(getattr(node, "lineno", 0) or 0)
                )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = getattr(node, "value", None)
            if value is None:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                ):
                    field = slice_text(target.slice)
                    if field:
                        field_assignments.setdefault(
                            (target.value.id, field),
                            [],
                        ).append((
                            int(getattr(node, "lineno", 0) or 0),
                            value,
                        ))
                    continue
                if not isinstance(target, ast.Name):
                    continue
                line = int(getattr(node, "lineno", 0) or 0)
                assignments.setdefault(target.id, []).append((line, value))
                if isinstance(value, (ast.Dict, ast.List, ast.Tuple)):
                    container_values[target.id] = value
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.args
            ):
                continue
            container_appends.setdefault(node.func.value.id, []).append((
                int(getattr(node, "lineno", 0) or 0),
                node.args[0],
            ))
        for values in assignments.values():
            values.sort(key=lambda item: item[0])
        for values in field_assignments.values():
            values.sort(key=lambda item: item[0])
        for values in container_appends.values():
            values.sort(key=lambda item: item[0])

        def latest_assignment(name: str, before_line: int) -> tuple[int, ast.AST] | None:
            candidates = [
                item for item in assignments.get(name, [])
                if item[0] < before_line
            ]
            return candidates[-1] if candidates else None

        def latest_field_assignment(
            frame: str,
            field: str,
            before_line: int,
        ) -> tuple[int, ast.AST] | None:
            candidates = [
                item for item in field_assignments.get((frame, field), [])
                if item[0] < before_line
            ]
            return candidates[-1] if candidates else None

        def root_name(node: ast.AST) -> str:
            current = node
            while isinstance(current, (ast.Attribute, ast.Subscript)):
                current = current.value
            return current.id if isinstance(current, ast.Name) else ""

        def field_signatures(
            expression: ast.AST,
            before_line: int,
            seen: set[tuple[str, int]] | None = None,
        ) -> set[str]:
            visited = set(seen or set())
            if isinstance(expression, ast.Name):
                candidates = [
                    item for item in assignments.get(expression.id, [])
                    if item[0] < before_line
                ]
                for line, value in reversed(candidates):
                    marker = (expression.id, line)
                    if marker in visited:
                        continue
                    signatures = field_signatures(
                        value,
                        line,
                        visited | {marker},
                    )
                    if signatures:
                        return signatures
                return set()
            if isinstance(expression, ast.Subscript):
                field = slice_text(expression.slice)
                root = root_name(expression.value)
                if root and field:
                    assignment = latest_field_assignment(root, field, before_line)
                    if assignment is not None:
                        marker = (f"{root}.{field}", assignment[0])
                        if marker not in visited:
                            resolved = field_signatures(
                                assignment[1],
                                assignment[0],
                                visited | {marker},
                            )
                            if resolved:
                                return resolved
                signatures = {f"{root}.{field}"} if root and field else set()
                return signatures
            signatures: set[str] = set()
            for child in ast.iter_child_nodes(expression):
                signatures.update(field_signatures(child, before_line, visited))
            return signatures

        def denominator_signatures(
            expression: ast.AST,
            before_line: int,
            seen: set[tuple[str, int]] | None = None,
        ) -> set[str]:
            visited = set(seen or set())
            if isinstance(expression, ast.Name):
                candidates = [
                    item for item in assignments.get(expression.id, [])
                    if item[0] < before_line
                ]
                for line, value in reversed(candidates):
                    marker = (expression.id, line)
                    if marker in visited:
                        continue
                    denominators = denominator_signatures(
                        value,
                        line,
                        visited | {marker},
                    )
                    if denominators:
                        return denominators
                return set()
            if isinstance(expression, ast.Subscript):
                field = slice_text(expression.slice)
                root = root_name(expression.value)
                if root and field:
                    assignment = latest_field_assignment(root, field, before_line)
                    if assignment is not None:
                        marker = (f"{root}.{field}", assignment[0])
                        if marker not in visited:
                            return denominator_signatures(
                                assignment[1],
                                assignment[0],
                                visited | {marker},
                            )
            denominators: set[str] = set()
            if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Div):
                denominator_fields = field_signatures(
                    expression.right,
                    before_line,
                    visited,
                )
                if denominator_fields:
                    denominators.add("fields:" + "|".join(sorted(denominator_fields)))
                elif isinstance(expression.right, ast.Name):
                    denominators.add(f"name:{expression.right.id}")
                else:
                    denominators.add(
                        "expr:" + ast.dump(expression.right, include_attributes=False)
                    )
            for child in ast.iter_child_nodes(expression):
                denominators.update(
                    denominator_signatures(child, before_line, visited)
                )
            return denominators

        def heterogeneous_member_details(
            members: list[tuple[str, ast.AST]],
            line: int,
            *,
            shared_denominator_is_comparable: bool = True,
            normalized_components_are_comparable: bool = False,
        ) -> list[dict[str, Any]]:
            details = []
            for label, expression in members:
                signatures = field_signatures(expression, line)
                if not signatures:
                    continue
                details.append({
                    "label": label,
                    "field_signatures": sorted(signatures),
                    "denominator_signatures": sorted(
                        denominator_signatures(expression, line)
                    ),
                })
            if len(details) < 2:
                return []
            field_families = {
                tuple(item["field_signatures"]) for item in details
            }
            if len(field_families) <= 1:
                return []
            denominator_sets = [
                set(item["denominator_signatures"]) for item in details
            ]
            if normalized_components_are_comparable and all(denominator_sets):
                return []
            if (
                shared_denominator_is_comparable
                and all(denominator_sets)
                and set.intersection(*denominator_sets)
            ):
                return []
            return details

        def additive_components(expression: ast.AST) -> list[ast.AST]:
            if isinstance(expression, ast.BinOp) and isinstance(
                expression.op,
                (ast.Add, ast.Sub),
            ):
                return [
                    *additive_components(expression.left),
                    *additive_components(expression.right),
                ]
            return [expression]

        def score_like_field(field: str) -> bool:
            normalized = str(field or "").strip().lower()
            return bool(re.search(
                r"(?:^|[_\-\s])(score|strength|priority|index)(?:$|[_\-\s])"
                r"|得分|评分|强度|优先级|综合指数",
                normalized,
            ))

        def dict_members(node: ast.Dict, value_field: str = "") -> list[tuple[str, ast.AST]]:
            members: list[tuple[str, ast.AST]] = []
            for index, (key, value) in enumerate(zip(node.keys, node.values)):
                label = (
                    str(key.value)
                    if isinstance(key, ast.Constant)
                    else f"member_{index + 1}"
                )
                if value_field and label != value_field:
                    continue
                members.append((label, value))
            return members

        def sorted_mapping_name(node: ast.AST) -> str:
            if not isinstance(node, ast.Call):
                return ""
            if not isinstance(node.func, ast.Name) or node.func.id != "sorted":
                return ""
            if not node.args:
                return ""
            source = node.args[0]
            if (
                isinstance(source, ast.Call)
                and isinstance(source.func, ast.Attribute)
                and source.func.attr == "items"
                and isinstance(source.func.value, ast.Name)
            ):
                return source.func.value.id
            return ""

        def sort_value_field(call: ast.Call) -> str:
            if call.args and isinstance(call.args[0], ast.Constant):
                return str(call.args[0].value or "").strip()
            if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)):
                return next((
                    str(item.value or "").strip()
                    for item in call.args[0].elts
                    if isinstance(item, ast.Constant)
                    and str(item.value or "").strip()
                ), "")
            for keyword in call.keywords:
                if keyword.arg == "by" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value or "").strip()
                if keyword.arg == "by" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    return next((
                        str(item.value or "").strip()
                        for item in keyword.value.elts
                        if isinstance(item, ast.Constant)
                        and str(item.value or "").strip()
                    ), "")
            return ""

        issues: list[dict[str, Any]] = []

        # A vectorized composite score has the same semantic boundary as a
        # row-wise score. Follow ``frame['score'] = frame['a'] + frame['b']``
        # through intermediate column assignments before a sort. Restrict this
        # to explicitly score-like fields so ordinary same-unit business totals
        # are not mistaken for an analytical ranking model.
        for (frame_name, score_field), score_values in field_assignments.items():
            if not score_like_field(score_field):
                continue
            for score_line, score_value in score_values:
                if (
                    isinstance(score_value, ast.Call)
                    and isinstance(score_value.func, ast.Attribute)
                    and score_value.func.attr == "apply"
                ):
                    continue
                components = additive_components(score_value)
                if len(components) < 2:
                    continue
                members = [
                    (f"component_{index + 1}", component)
                    for index, component in enumerate(components)
                ]
                details = heterogeneous_member_details(
                    members,
                    score_line,
                    shared_denominator_is_comparable=False,
                    normalized_components_are_comparable=True,
                )
                if not details:
                    continue
                ranking_lines = [
                    int(getattr(call, "lineno", 0) or 0)
                    for call in ast.walk(tree)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "sort_values"
                    and root_name(call.func.value) == frame_name
                    and sort_value_field(call) == score_field
                ]
                ranking_lines = [line for line in ranking_lines if line >= score_line]
                if not ranking_lines:
                    continue
                issues.append({
                    "line": min(ranking_lines),
                    "container": frame_name,
                    "result_frame": frame_name,
                    "value_field": score_field,
                    "score_function": "vectorized_composite_score",
                    "member_boundaries": details,
                })

        # A custom row-wise score is still one ranked measure. Follow every
        # component used by the score function back to the DataFrame fields.
        # Distinct raw boundaries require explicit normalization; a shared
        # denominator is sufficient proof that the components are comparable.
        for (frame_name, score_field), score_values in field_assignments.items():
            for score_line, score_value in score_values:
                if not (
                    isinstance(score_value, ast.Call)
                    and isinstance(score_value.func, ast.Attribute)
                    and score_value.func.attr == "apply"
                    and isinstance(score_value.func.value, ast.Name)
                    and score_value.func.value.id == frame_name
                    and score_value.args
                    and isinstance(score_value.args[0], ast.Name)
                ):
                    continue
                function = function_definitions.get(score_value.args[0].id)
                if function is None or not function.args.args:
                    continue
                row_name = function.args.args[0].arg
                component_fields = sorted({
                    slice_text(item.slice)
                    for item in ast.walk(function)
                    if isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Name)
                    and item.value.id == row_name
                    and slice_text(item.slice)
                })
                members: list[tuple[str, ast.AST]] = []
                for component_field in component_fields:
                    assignment = latest_field_assignment(
                        frame_name,
                        component_field,
                        score_line,
                    )
                    expression = (
                        assignment[1]
                        if assignment is not None
                        else ast.Subscript(
                            value=ast.Name(id=frame_name, ctx=ast.Load()),
                            slice=ast.Constant(value=component_field),
                            ctx=ast.Load(),
                        )
                    )
                    members.append((component_field, expression))
                details = heterogeneous_member_details(
                    members,
                    score_line,
                    normalized_components_are_comparable=True,
                )
                if not details:
                    continue
                ranking_lines = [
                    int(getattr(call, "lineno", 0) or 0)
                    for call in ast.walk(tree)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "sort_values"
                    and root_name(call.func.value) == frame_name
                    and sort_value_field(call) == score_field
                ]
                ranking_lines = [line for line in ranking_lines if line >= score_line]
                if not ranking_lines:
                    continue
                issues.append({
                    "line": min(ranking_lines),
                    "container": frame_name,
                    "result_frame": frame_name,
                    "value_field": score_field,
                    "score_function": score_value.args[0].id,
                    "member_boundaries": details,
                })

        # The same row-wise score is often written without ``DataFrame.apply``:
        # iterate an aggregate frame, calculate a local score, append records,
        # rebuild a DataFrame, then sort it. Follow that ordinary Python form
        # back to the source columns so changing syntax cannot bypass the
        # comparable-measure boundary.
        for loop in (node for node in ast.walk(tree) if isinstance(node, ast.For)):
            iterator = loop.iter
            target = loop.target
            if (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Name)
                and iterator.func.id == "enumerate"
                and iterator.args
            ):
                iterator = iterator.args[0]
                if isinstance(target, (ast.Tuple, ast.List)) and len(target.elts) >= 2:
                    target = target.elts[1]
            if not (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Attribute)
                and iterator.func.attr == "iterrows"
            ):
                continue
            source_frame = root_name(iterator.func.value)
            target_names = [
                item.id
                for item in ast.walk(target)
                if isinstance(item, ast.Name)
            ]
            row_name = target_names[-1] if target_names else ""
            if not source_frame or not row_name:
                continue

            local_assignments: dict[str, tuple[int, ast.AST]] = {}
            for statement in ast.walk(loop):
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                statement_targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                value = getattr(statement, "value", None)
                if value is None:
                    continue
                for statement_target in statement_targets:
                    if isinstance(statement_target, ast.Name):
                        local_assignments[statement_target.id] = (
                            int(getattr(statement, "lineno", 0) or 0),
                            value,
                        )

            for append_call in (
                node for node in ast.walk(loop)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.args
                and isinstance(node.args[0], ast.Dict)
            ):
                container_name = append_call.func.value.id
                record = append_call.args[0]
                record_fields = {
                    str(key.value): value
                    for key, value in zip(record.keys, record.values)
                    if isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                }
                for score_field, score_reference in record_fields.items():
                    score_expression = score_reference
                    score_name = ""
                    if isinstance(score_reference, ast.Name):
                        score_name = score_reference.id
                        assignment = local_assignments.get(score_name)
                        if assignment is None:
                            continue
                        score_expression = assignment[1]
                    component_fields = sorted({
                        slice_text(item.slice)
                        for item in ast.walk(score_expression)
                        if isinstance(item, ast.Subscript)
                        and isinstance(item.value, ast.Name)
                        and item.value.id == row_name
                        and slice_text(item.slice)
                    })
                    if len(component_fields) < 2:
                        continue
                    members = [
                        (
                            component_field,
                            ast.Subscript(
                                value=ast.Name(id=source_frame, ctx=ast.Load()),
                                slice=ast.Constant(value=component_field),
                                ctx=ast.Load(),
                            ),
                        )
                        for component_field in component_fields
                    ]
                    details = heterogeneous_member_details(
                        members,
                        int(getattr(append_call, "lineno", 0) or 0),
                        normalized_components_are_comparable=True,
                    )
                    if not details:
                        continue

                    ranking_match: tuple[int, str] | None = None
                    for assignment_node in (
                        item for item in ast.walk(tree)
                        if isinstance(item, (ast.Assign, ast.AnnAssign))
                    ):
                        assignment_line = int(getattr(assignment_node, "lineno", 0) or 0)
                        if assignment_line <= int(getattr(loop, "end_lineno", 0) or 0):
                            continue
                        assignment_value = getattr(assignment_node, "value", None)
                        if assignment_value is None:
                            continue
                        dataframe_call = next((
                            call for call in ast.walk(assignment_value)
                            if isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "DataFrame"
                            and call.args
                            and isinstance(call.args[0], ast.Name)
                            and call.args[0].id == container_name
                        ), None)
                        sort_call = next((
                            call for call in ast.walk(assignment_value)
                            if isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "sort_values"
                            and sort_value_field(call) == score_field
                        ), None)
                        if dataframe_call is None or sort_call is None:
                            continue
                        assignment_targets = (
                            assignment_node.targets
                            if isinstance(assignment_node, ast.Assign)
                            else [assignment_node.target]
                        )
                        result_frame = next((
                            item.id for item in assignment_targets
                            if isinstance(item, ast.Name)
                        ), "")
                        if result_frame:
                            ranking_match = (assignment_line, result_frame)
                            break
                    if ranking_match is None:
                        continue
                    issues.append({
                        "line": ranking_match[0],
                        "container": container_name,
                        "result_frame": ranking_match[1],
                        "value_field": score_field,
                        "score_function": score_name or "inline_row_score",
                        "member_boundaries": details,
                    })

        # In-place list.sort(...) is equivalent to assigning sorted(...), but
        # has no result variable. Follow tuple/list members selected by the key
        # lambda so this ordinary Python form receives the same boundary check.
        for call in (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sort"
            and isinstance(node.func.value, ast.Name)
        ):
            container_name = call.func.value.id
            source = container_values.get(container_name)
            if not isinstance(source, (ast.List, ast.Tuple)):
                continue
            line = int(getattr(call, "lineno", 0) or 0)
            key_lambda = next((
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "key" and isinstance(keyword.value, ast.Lambda)
            ), None)
            member_index = 0
            if (
                isinstance(key_lambda, ast.Lambda)
                and isinstance(key_lambda.body, ast.Subscript)
                and isinstance(key_lambda.body.slice, ast.Constant)
                and isinstance(key_lambda.body.slice.value, int)
            ):
                member_index = int(key_lambda.body.slice.value)
            members: list[tuple[str, ast.AST]] = []
            member_nodes = list(source.elts)
            member_nodes.extend(
                value
                for append_line, value in container_appends.get(container_name, [])
                if append_line < line
            )
            for index, item in enumerate(member_nodes):
                if isinstance(item, (ast.List, ast.Tuple)):
                    if -len(item.elts) <= member_index < len(item.elts):
                        members.append((
                            f"member_{index + 1}",
                            item.elts[member_index],
                        ))
                elif member_index == 0:
                    members.append((f"member_{index + 1}", item))
            details = heterogeneous_member_details(members, line)
            if details and any(
                loaded_line > line
                for loaded_line in loaded_lines.get(container_name, [])
            ):
                issues.append({
                    "line": line,
                    "container": container_name,
                    "result_frame": container_name,
                    "value_field": f"tuple_index_{member_index}",
                    "member_boundaries": details,
                })

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            result_names = [target.id for target in targets if isinstance(target, ast.Name)]
            if len(result_names) != 1:
                continue
            value = getattr(node, "value", None)
            line = int(getattr(node, "lineno", 0) or 0)
            if not any(
                loaded_line > line
                for loaded_line in loaded_lines.get(result_names[0], [])
            ):
                continue
            mapping_name = sorted_mapping_name(value)
            mapping = container_values.get(mapping_name)
            if mapping_name and isinstance(mapping, ast.Dict):
                details = heterogeneous_member_details(dict_members(mapping), line)
                if details:
                    issues.append({
                        "line": line,
                        "container": mapping_name,
                        "result_frame": result_names[0],
                        "value_field": "value",
                        "member_boundaries": details,
                    })
                continue

            sort_call = next(
                (
                    child for child in ast.walk(value)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "sort_values"
                ),
                None,
            )
            if sort_call is None:
                continue
            value_field = sort_value_field(sort_call)
            if not value_field:
                continue
            dataframe_expression = value
            sort_source = sort_call.func.value
            if isinstance(sort_source, ast.Name):
                source_assignment = latest_assignment(sort_source.id, line)
                if source_assignment is not None:
                    dataframe_expression = source_assignment[1]
            dataframe_call = next(
                (
                    child for child in ast.walk(dataframe_expression)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "DataFrame"
                ),
                None,
            )
            if dataframe_call is None or not dataframe_call.args:
                continue
            source = dataframe_call.args[0]
            if isinstance(source, ast.Name):
                source = container_values.get(source.id) or source
            if not isinstance(source, (ast.List, ast.Tuple)):
                continue
            members = []
            for index, item in enumerate(source.elts):
                if isinstance(item, ast.Dict):
                    row = {
                        str(key.value): value
                        for key, value in zip(item.keys, item.values)
                        if isinstance(key, ast.Constant)
                    }
                    if value_field in row:
                        members.append((f"row_{index + 1}", row[value_field]))
                    continue
                if not isinstance(item, (ast.List, ast.Tuple)):
                    continue
                columns_node = next(
                    (
                        keyword.value
                        for keyword in dataframe_call.keywords
                        if keyword.arg == "columns"
                    ),
                    None,
                )
                columns = [
                    str(column.value)
                    for column in columns_node.elts
                    if isinstance(column, ast.Constant)
                ] if isinstance(columns_node, (ast.List, ast.Tuple)) else []
                if value_field not in columns:
                    continue
                value_index = columns.index(value_field)
                if value_index >= len(item.elts):
                    continue
                label = (
                    str(item.elts[0].value)
                    if item.elts
                    and isinstance(item.elts[0], ast.Constant)
                    else f"row_{index + 1}"
                )
                members.append((label, item.elts[value_index]))
            details = heterogeneous_member_details(members, line)
            if details:
                issues.append({
                    "line": line,
                    "container": (
                        dataframe_call.args[0].id
                        if isinstance(dataframe_call.args[0], ast.Name)
                        else "inline_rows"
                    ),
                    "result_frame": result_names[0],
                    "value_field": value_field,
                    "member_boundaries": details,
                })
        unique_issues: list[dict[str, Any]] = []
        seen_issues: set[tuple[int, str, str]] = set()
        for issue in issues:
            signature = (
                int(issue.get("line") or 0),
                str(issue.get("result_frame") or ""),
                str(issue.get("value_field") or ""),
            )
            if signature in seen_issues:
                continue
            seen_issues.add(signature)
            unique_issues.append(issue)
        return unique_issues

    @staticmethod
    def _mark_mapping_metric_mixtures(
        outputs: list[dict[str, Any]],
        *,
        code: str,
    ) -> list[dict[str, Any]]:
        """Mark mapping-to-DataFrame columns that mix unrelated fact fields."""
        try:
            tree = ast.parse(str(code or ""))
        except SyntaxError:
            return outputs

        member_fields: dict[str, list[set[str]]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            value = getattr(node, "value", None)
            if value is None:
                continue
            for target in targets:
                if not (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    continue
                fields = {
                    str(item.slice.value).strip()
                    for item in ast.walk(value)
                    if isinstance(item, ast.Subscript)
                    and isinstance(item.slice, ast.Constant)
                    and isinstance(item.slice.value, str)
                    and str(item.slice.value).strip()
                }
                if fields:
                    member_fields.setdefault(target.value.id, []).append(fields)

        frame_mappings: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            frame_names = [item.id for item in targets if isinstance(item, ast.Name)]
            value = getattr(node, "value", None)
            if len(frame_names) != 1 or value is None:
                continue
            mapping_names = {
                call.func.value.id
                for call in ast.walk(value)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "items"
                and isinstance(call.func.value, ast.Name)
            }
            if len(mapping_names) == 1:
                frame_mappings[frame_names[0]] = next(iter(mapping_names))

        mixed_frames: dict[str, list[list[str]]] = {}
        for frame_name, mapping_name in frame_mappings.items():
            signatures = {
                tuple(sorted(fields))
                for fields in member_fields.get(mapping_name, [])
                if fields
            }
            if len(signatures) > 1:
                mixed_frames[frame_name] = [list(item) for item in sorted(signatures)]
        if not mixed_frames:
            return outputs

        enriched: list[dict[str, Any]] = []
        for raw_output in outputs:
            output = dict(raw_output)
            source_frame = str(output.get("source_frame") or "").strip()
            if (
                source_frame in mixed_frames
                and output.get("artifact_type") in {"chart", "table"}
            ):
                output["ambiguous_calculation_boundary"] = True
                output["heterogeneous_structured_value"] = True
                output["heterogeneous_mapping_field_signatures"] = mixed_frames[source_frame]
            enriched.append(output)
        return enriched

    @classmethod
    def _source_expression_catalog(cls, code: str) -> list[dict[str, Any]]:
        """Index dynamic string expressions without deciding report ownership.

        This is a syntax catalog for the planner and verifier, not a list of
        report metrics. Expressions from chart descriptions, diagnostics, and
        other strings are intentionally present so semantic ownership remains
        the planner's responsibility.
        """
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return []
        formatted_numeric_names = cls._formatted_numeric_names(tree)
        comprehension_scoped_loads: dict[int, dict[str, str]] = {}
        for comprehension in (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
        ):
            line = int(getattr(comprehension, "lineno", 0) or 0)
            if line <= 0:
                continue
            scoped = comprehension_scoped_loads.setdefault(line, {})
            for generator in comprehension.generators:
                for name in (
                    item.id
                    for item in ast.walk(generator.target)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                ):
                    scoped[name] = f"{name}@comprehension:{line}"
        templates: list[ast.JoinedStr] = []
        templates.extend(
            item for item in ast.walk(tree) if isinstance(item, ast.JoinedStr)
        )
        for item in ast.walk(tree):
            if isinstance(item, ast.Call):
                template = cls._format_call_template(item)
                if template is not None:
                    templates.append(template)
            elif isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
                template = cls._concatenated_string_template(item)
                if template is not None:
                    templates.append(template)

        anchors: dict[str, dict[str, Any]] = {}
        for template in templates:
            for expression in cls._joined_string_expressions(
                template,
                code,
                formatted_numeric_names=formatted_numeric_names,
            ):
                anchor_id = str(expression.get("expression_id") or "")
                if not anchor_id:
                    continue
                scoped_loads = comprehension_scoped_loads.get(
                    int(expression.get("line") or 0),
                    {},
                )
                if scoped_loads:
                    expression["loads"] = [
                        scoped_loads.get(str(name), str(name))
                        for name in expression.get("loads") or []
                    ]
                anchors.setdefault(anchor_id, {
                    "anchor_id": anchor_id,
                    "source_line": int(expression.get("line") or 0),
                    "expression": str(expression.get("expression") or ""),
                    "loads": list(expression.get("loads") or []),
                    "display_prefix": str(expression.get("prefix") or ""),
                    "display_suffix": str(expression.get("suffix") or ""),
                    "numeric_hint": bool(expression.get("numeric_hint")),
                    "numeric_suffix": str(expression.get("numeric_suffix") or ""),
                    "presentation_ordinal": bool(
                        expression.get("presentation_ordinal")
                    ),
                    "expression_metadata": expression,
                })
        return sorted(
            anchors.values(),
            key=lambda item: (
                int(item.get("source_line") or 0),
                str(item.get("anchor_id") or ""),
            ),
        )

    @staticmethod
    def _report_construction_issues(code: str) -> list[dict[str, Any]]:
        """Describe saved-report expressions whose provenance cannot be proven statically."""
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return [{"reason": "generated_code_syntax_error"}]

        assignments: dict[str, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            value = getattr(node, "value", None)
            if value is None:
                continue
            for name in _target_names(node):
                if isinstance(name, str) and name.isidentifier():
                    assignments.setdefault(name, []).append(value)

        def supported_join_sequence(value: ast.AST | None) -> bool:
            if isinstance(value, ast.Name):
                return True
            if isinstance(value, (ast.List, ast.Tuple)):
                return all(supported(item, set()) for item in value.elts)
            if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
                return (
                    supported_join_sequence(value.left)
                    and supported_join_sequence(value.right)
                )
            if isinstance(value, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                return True
            return False

        def supported(value: ast.AST | None, visited: set[str]) -> bool:
            if isinstance(value, ast.Constant):
                return isinstance(value.value, str)
            if isinstance(value, ast.JoinedStr):
                return True
            if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
                return supported(value.left, visited) and supported(value.right, visited)
            if isinstance(value, ast.IfExp):
                return supported(value.body, visited) and supported(value.orelse, visited)
            if isinstance(value, ast.Name):
                if value.id in visited:
                    return True
                values = assignments.get(value.id) or []
                return bool(values) and all(
                    supported(item, {*visited, value.id}) for item in values
                )
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr == "format":
                    return LineageIndexer._format_call_template(value) is not None
                if value.func.attr == "join" and value.args:
                    return supported_join_sequence(value.args[0])
            return False

        issues: list[dict[str, Any]] = []
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            function_name = ""
            if isinstance(call.func, ast.Name):
                function_name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                function_name = call.func.attr
            if function_name != "save_analysis_result":
                continue
            report_value = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "analysis_report"),
                None,
            )
            if report_value is not None and supported(report_value, set()):
                continue
            issues.append({
                "reason": "unsupported_saved_report_construction",
                "line": int(getattr(call, "lineno", 0) or 0),
                "expression": (
                    ast.get_source_segment(code, report_value)
                    if report_value is not None
                    else ""
                ) or (ast.unparse(report_value) if report_value is not None else ""),
            })
        return issues

    @staticmethod
    def _literal_iteration_bindings(code: str) -> list[dict[str, Any]]:
        """Return correlated loop-variable values from literal tuple sequences."""
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return []
        sequences: dict[str, list[list[Any]]] = {}
        for assignment in (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.Assign, ast.AnnAssign))
        ):
            value = getattr(assignment, "value", None)
            targets = getattr(assignment, "targets", None) or [getattr(assignment, "target", None)]
            names = [item.id for item in targets if isinstance(item, ast.Name)]
            if len(names) != 1 or not isinstance(value, (ast.List, ast.Tuple)):
                continue
            rows: list[list[Any]] = []
            for row in value.elts:
                if not isinstance(row, (ast.List, ast.Tuple)):
                    rows = []
                    break
                values: list[Any] = []
                for cell in row.elts:
                    try:
                        values.append(ast.literal_eval(cell))
                    except (ValueError, TypeError, SyntaxError):
                        values = []
                        break
                if not values:
                    rows = []
                    break
                rows.append(values)
            if rows:
                sequences[names[0]] = rows

        bindings: list[dict[str, Any]] = []
        for loop in (item for item in ast.walk(tree) if isinstance(item, ast.For)):
            if not (
                isinstance(loop.target, (ast.Tuple, ast.List))
                and isinstance(loop.iter, ast.Name)
                and loop.iter.id in sequences
            ):
                continue
            target_names = [
                item.id if isinstance(item, ast.Name) else ""
                for item in loop.target.elts
            ]
            if not target_names or any(not item for item in target_names):
                continue
            rows = sequences[loop.iter.id]
            if any(len(row) != len(target_names) for row in rows):
                continue
            bindings.append({
                "sequence_name": loop.iter.id,
                "line": int(getattr(loop, "lineno", 0) or 0),
                "rows": [dict(zip(target_names, row)) for row in rows],
            })
        return bindings

    @staticmethod
    def _lineage_query_snapshots(query_snapshots: dict[str, Any]) -> dict[str, Any]:
        materialized = {
            str(key): value
            for key, value in query_snapshots.items()
            if isinstance(value, dict)
            and str((value.get("execution") or {}).get("status") or "")
            in {"success", "no_data"}
        }
        return materialized or query_snapshots

    @staticmethod
    def _query_operation_nodes(query_snapshots: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for query_key, raw_snapshot in query_snapshots.items():
            snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
            implementation = snapshot.get("implementation") if isinstance(snapshot.get("implementation"), dict) else {}
            operation_payload = {
                "query_key": str(query_key),
                "analysis_scope": snapshot.get("analysis_scope") or {},
                "query_scope": snapshot.get("query_scope") or {},
                "query_constraints": snapshot.get("query_constraints") or {},
                "sql": implementation.get("sql") or "",
                "loader": implementation.get("loader") or "",
                "datasource_identifier": implementation.get("datasource_identifier") or "",
                "loader_options": implementation.get("loader_options") or {},
                "filter_expressions": implementation.get("filter_expressions") or [],
                "file_filters": implementation.get("file_filters") or [],
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
            execution = (
                snapshot.get("execution")
                if isinstance(snapshot.get("execution"), dict)
                else {}
            )
            row_count = execution.get("row_count")
            if (
                isinstance(row_count, int)
                and not isinstance(row_count, bool)
                and str(execution.get("status") or "") in {"success", "no_data"}
            ):
                row_count_identity = _stable_json({
                    "query_key": str(query_key),
                    "metric": "query_result_row_count",
                })
                nodes.append({
                    "operation_node_id": (
                        f"op_query_row_count_{_digest(row_count_identity, 12)}"
                    ),
                    "line": 0,
                    "kind": "query_result_row_count",
                    "semantic_kind": "aggregation",
                    "projection_role": "measure",
                    "query_node_id": str(query_key),
                    "targets": [f"query_result[{query_key!r}]['row_count']"],
                    "loads": [],
                    "source": "QuerySnapshot.execution.row_count",
                    "observed_value": row_count,
                })
            sql_projection_nodes = LineageIndexer._sql_projection_nodes(
                query_key=str(query_key),
                sql=str(implementation.get("sql") or ""),
                sql_truncated=bool(implementation.get("sql_truncated")),
                projection_semantics=(
                    implementation.get("projection_semantics")
                    if isinstance(implementation.get("projection_semantics"), list)
                    else []
                ),
            )
            nodes.extend(sql_projection_nodes)
            projected_fields = {
                str(field)
                for operation in sql_projection_nodes
                for field in operation.get("targets") or []
                if str(field)
            }
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
    def _sql_projection_nodes(
        *,
        query_key: str,
        sql: str,
        sql_truncated: bool = False,
        projection_semantics: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if projection_semantics:
            nodes: list[dict[str, Any]] = []
            for index, item in enumerate(projection_semantics):
                if not isinstance(item, dict):
                    continue
                alias = str(item.get("alias") or "").strip()
                semantic_kind = str(item.get("semantic_kind") or "projection").strip()
                expression_source = str(item.get("expression") or alias).strip()
                if not alias:
                    continue
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
                    "projection_role": str(item.get("projection_role") or "unknown"),
                    "query_node_id": query_key,
                    "targets": [alias],
                    "loads": [],
                    "source": expression_source[:2000],
                })
            if nodes:
                return nodes
        if not sql.strip() or sql_truncated:
            return []
        try:
            statement = parse_one(sql)
        except (ParseError, TokenError):
            parameterized_sql = re.sub(r"%\([^)]+\)s", "0", sql)
            parameterized_sql = re.sub(r"(?<!%)%s", "0", parameterized_sql)
            try:
                statement = parse_one(parameterized_sql)
            except (ParseError, TokenError):
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
        list[dict[str, Any]],
        list[dict[str, str]],
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
        report_template_metadata, synthetic_report_templates = self._report_template_metadata(
            tree,
            code,
        )
        comprehension_scoped_loads: dict[int, dict[str, str]] = {}
        for comprehension in (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
        ):
            line = int(getattr(comprehension, "lineno", 0) or 0)
            if line <= 0:
                continue
            scoped = comprehension_scoped_loads.setdefault(line, {})
            for generator in comprehension.generators:
                for name in (
                    item.id
                    for item in ast.walk(generator.target)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                ):
                    scoped[name] = f"{name}@comprehension:{line}"
        joined_strings_in_report_expressions: set[int] = set()
        synthetic_concatenation_joined_strings: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = getattr(node, "value", None)
            if "analysis_report" not in _target_names(node) or value is None:
                continue
            joined_strings_in_report_expressions.update(
                id(item) for item in ast.walk(value) if isinstance(item, ast.JoinedStr)
            )
            if not isinstance(value, ast.BinOp):
                continue
            template = self._concatenated_string_template(value)
            contains_joined_builder = any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "join"
                for item in ast.walk(value)
            )
            if template is None or contains_joined_builder:
                continue
            synthetic_concatenation_joined_strings.update(
                id(item) for item in ast.walk(value) if isinstance(item, ast.JoinedStr)
            )
            expressions = self._joined_string_expressions(
                template,
                code,
                formatted_numeric_names=formatted_numeric_names,
            )
            for expression in expressions:
                expression["report_declared"] = True
            report_expressions.extend(expressions)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                source = ast.get_source_segment(code, node) or ast.unparse(node)
                has_operation = bool(_called_operation_names(node) & self._OPERATION_CALLS)
                has_formula = any(isinstance(item, ast.BinOp) for item in ast.walk(node))
                has_formatted_value = any(
                    isinstance(item, ast.FormattedValue) for item in ast.walk(node)
                )
                assignment_value = getattr(node, "value", None)
                has_container_write = isinstance(assignment_value, ast.Dict) and any(
                    isinstance(item, ast.Subscript) and isinstance(item.ctx, ast.Store)
                    for item in ast.walk(node)
                )
                query_node_id = self._load_query_node_id(node, query_bindings)
                has_projection = self._is_projection_assignment(node)
                if (
                    has_operation
                    or has_formula
                    or has_formatted_value
                    or has_container_write
                    or query_node_id
                    or has_projection
                ):
                    semantic_kind = (
                        "presentation"
                        if has_container_write
                        else self._operation_semantic_kind(node, query_node_id=query_node_id)
                    )
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
                else:
                    targets = _target_names(node)
                    loads = _loaded_names(node)
                    if targets and loads:
                        operations.append({
                            "operation_node_id": (
                                f"op_stmt_{getattr(node, 'lineno', 0)}_{_digest(source, 8)}"
                            ),
                            "line": getattr(node, "lineno", 0),
                            "kind": "code_assignment",
                            "semantic_kind": "transformation",
                            "query_node_id": "",
                            "targets": targets,
                            "loads": loads,
                            "source": source[:1200],
                        })
                operations.extend(self._dataframe_literal_series_operations(node, code))
                operations.extend(self._mapping_literal_member_operations(node, code))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Dict)
            ):
                container_name = node.func.value.id
                for key_node, value_node in zip(node.args[0].keys, node.args[0].values):
                    if not (
                        isinstance(key_node, ast.Constant)
                        and isinstance(key_node.value, str)
                    ):
                        continue
                    field = str(key_node.value)
                    value_source = ast.get_source_segment(code, value_node) or ast.unparse(value_node)
                    target = f"{container_name}[{field!r}]"
                    source = f"{target} = {value_source}"
                    semantic_kind = (
                        "formula"
                        if any(
                            isinstance(item, (ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp))
                            for item in ast.walk(value_node)
                        )
                        else "presentation"
                    )
                    operations.append({
                        "operation_node_id": (
                            f"op_record_write_{getattr(value_node, 'lineno', 0)}_"
                            f"{_digest(source, 8)}"
                        ),
                        "line": getattr(value_node, "lineno", 0),
                        "kind": "calculation",
                        "semantic_kind": semantic_kind,
                        "query_node_id": "",
                        "targets": [target],
                        "loads": _loaded_names(value_node),
                        "source": source[:1200],
                    })
            if (
                isinstance(node, ast.JoinedStr)
                and id(node) not in synthetic_concatenation_joined_strings
            ):
                expressions = self._joined_string_expressions(
                    node,
                    code,
                    formatted_numeric_names=formatted_numeric_names,
                )
                for expression in expressions:
                    metadata = report_template_metadata.get(
                        str(expression.get("template_id") or "")
                    )
                    if metadata:
                        expression.update(metadata)
                    if id(node) in joined_strings_in_report_expressions:
                        expression["report_declared"] = True
                report_expressions.extend(expressions)
        operations.extend(self._container_iteration_operations(tree, code))
        report_expressions = [
            expression
            for expression in report_expressions
            if expression.get("report_declared")
        ]
        for template, metadata in synthetic_report_templates:
            expressions = self._joined_string_expressions(
                template,
                code,
                formatted_numeric_names=formatted_numeric_names,
            )
            for expression in expressions:
                expression.update(metadata)
            report_expressions.extend(expressions)
        for expression in report_expressions:
            scoped_loads = comprehension_scoped_loads.get(
                int(expression.get("line") or 0),
                {},
            )
            if scoped_loads:
                expression["loads"] = [
                    scoped_loads.get(str(name), str(name))
                    for name in expression.get("loads") or []
                ]
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

    @staticmethod
    def _container_iteration_operations(
        tree: ast.AST,
        code: str,
    ) -> list[dict[str, Any]]:
        """Connect values yielded by mapping.items() to writes into that mapping."""
        writes_by_container: dict[str, list[str]] = {}
        aliases_to_member_targets: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            target = getattr(node, "target", None)
            if target is None and isinstance(node, ast.Assign):
                targets = list(node.targets)
            else:
                targets = [target] if target is not None else []
            for item in targets:
                if not (
                    isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Name)
                    and isinstance(item.ctx, ast.Store)
                ):
                    continue
                writes_by_container.setdefault(item.value.id, []).append(ast.unparse(item))
            value = getattr(node, "value", None)
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and isinstance(value, ast.Dict)
            ):
                member_targets = [
                    f"{targets[0].id}[{key.value!r}]"
                    for key in value.keys
                    if isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                ]
                if member_targets:
                    writes_by_container.setdefault(targets[0].id, []).extend(
                        member_targets
                    )
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and isinstance(getattr(node, "value", None), ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "get"
                and isinstance(node.value.func.value, ast.Name)
                and node.value.args
                and isinstance(node.value.args[0], ast.Constant)
                and isinstance(node.value.args[0].value, str)
            ):
                aliases_to_member_targets[targets[0].id] = [
                    f"{node.value.func.value.id}[{node.value.args[0].value!r}]"
                ]

        def items_container(expression: ast.AST) -> str:
            calls = [
                item for item in ast.walk(expression)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "items"
                and isinstance(item.func.value, ast.Name)
            ]
            return calls[0].func.value.id if len(calls) == 1 else ""

        def append_iteration(
            *,
            target: ast.AST,
            iterator: ast.AST,
            line: int,
        ) -> None:
            if not isinstance(target, (ast.Tuple, ast.List)) or len(target.elts) < 2:
                return
            value_target = target.elts[1]
            if not isinstance(value_target, ast.Name):
                return
            container_name = items_container(iterator)
            if not container_name:
                return
            source_targets = list(dict.fromkeys(
                aliases_to_member_targets.get(container_name)
                or writes_by_container.get(container_name)
                or [container_name]
            ))
            source = ast.get_source_segment(code, iterator) or ast.unparse(iterator)
            operations.append({
                "operation_node_id": f"op_container_iter_{line}_{_digest(source, 8)}",
                "line": line,
                "kind": "container_iteration",
                "semantic_kind": "presentation",
                "query_node_id": "",
                "targets": [
                    value_target.id,
                    f"{value_target.id}@comprehension:{line}",
                ],
                "loads": source_targets,
                "source": source[:1200],
            })

        def append_dataframe_iteration(node: ast.For) -> None:
            if not (
                isinstance(node.target, (ast.Tuple, ast.List))
                and len(node.target.elts) >= 2
                and isinstance(node.target.elts[1], ast.Name)
                and isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Attribute)
                and node.iter.func.attr in {"iterrows", "itertuples"}
                and isinstance(node.iter.func.value, ast.Name)
            ):
                return
            frame_name = node.iter.func.value.id
            source = ast.get_source_segment(code, node.iter) or ast.unparse(node.iter)
            operations.append({
                "operation_node_id": (
                    f"op_frame_iter_{getattr(node, 'lineno', 0)}_{_digest(source, 8)}"
                ),
                "line": getattr(node, "lineno", 0),
                "kind": "container_iteration",
                "semantic_kind": "presentation",
                "query_node_id": "",
                "targets": [node.target.elts[1].id],
                "loads": [frame_name],
                "source": source[:1200],
            })

        operations: list[dict[str, Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                append_iteration(target=node.target, iterator=node.iter, line=int(node.lineno or 0))
                append_dataframe_iteration(node)
            elif isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
                for generator in node.generators:
                    append_iteration(
                        target=generator.target,
                        iterator=generator.iter,
                        line=int(getattr(node, "lineno", 0) or 0),
                    )
        return operations

    @classmethod
    def _report_template_metadata(
        cls,
        tree: ast.AST,
        code: str,
    ) -> tuple[dict[str, dict[str, Any]], list[tuple[ast.JoinedStr, dict[str, Any]]]]:
        """Identify f-strings that actually build the saved report.

        Generic f-strings used by chart descriptions or diagnostics must never be
        matched against rendered Markdown. For list-built reports, static append
        calls also provide deterministic section bounds for repeated row layouts.
        """

        report_value_names = {"analysis_report"}
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            for keyword in call.keywords:
                if keyword.arg == "analysis_report" and isinstance(keyword.value, ast.Name):
                    report_value_names.add(keyword.value.id)

        report_builders: set[str] = set()
        builder_owner_anchors: dict[str, tuple[list[str], list[str]]] = {}
        iterated_builder_links: list[tuple[str, str, int, int]] = []
        direct_templates: list[ast.JoinedStr] = []
        direct_synthetic_templates: list[ast.JoinedStr] = []
        joined_builders_by_name: dict[str, set[str]] = {}

        def joined_builder_names(call: ast.AST | None) -> set[str]:
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "join"
                and call.args
            ):
                return set()
            joined_value = call.args[0]
            if isinstance(joined_value, ast.Name):
                return {joined_value.id}
            if isinstance(joined_value, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                generators = list(joined_value.generators)
                if generators and isinstance(generators[0].iter, ast.Name):
                    return {generators[0].iter.id}
            if isinstance(joined_value, ast.BinOp):
                return {
                    item.id
                    for item in ast.walk(joined_value)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
                }
            return set()

        def joined_builder_name(call: ast.AST | None) -> str:
            names = joined_builder_names(call)
            return next(iter(names)) if len(names) == 1 else ""

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            value = getattr(node, "value", None)
            joined_builders = {
                builder_name
                for item in ast.walk(value) if value is not None
                for builder_name in joined_builder_names(item)
            }
            if joined_builders:
                for name in _target_names(node):
                    joined_builders_by_name.setdefault(name, set()).update(joined_builders)
            if not (set(_target_names(node)) & report_value_names):
                continue
            direct_templates.extend(
                item for item in ast.walk(value)
                if isinstance(item, ast.JoinedStr)
            )
            if isinstance(value, ast.Call):
                synthetic = cls._format_call_template(value)
                if synthetic is not None:
                    direct_synthetic_templates.append(synthetic)
            report_builders.update(joined_builder_names(value))
            for call in (
                item for item in ast.walk(value)
                if isinstance(item, ast.Call)
            ):
                report_builders.update(joined_builder_names(call))

        owned_fragment_names = {
            loaded_name
            for template in [*direct_templates, *direct_synthetic_templates]
            for loaded_name in _loaded_names(template)
        }
        for fragment_name in owned_fragment_names:
            report_builders.update(joined_builders_by_name.get(fragment_name) or set())
        for template in [*direct_templates, *direct_synthetic_templates]:
            for expression in cls._joined_string_expressions(template, code):
                fragment_name = str(expression.get("expression") or "").strip()
                if fragment_name not in joined_builders_by_name:
                    continue
                before = [str(expression.get("prefix") or "")]
                after = [str(expression.get("suffix") or "")]
                before = [item for item in before if item.strip()]
                after = [item for item in after if item.strip()]
                for builder_name in joined_builders_by_name[fragment_name]:
                    builder_owner_anchors[builder_name] = (before, after)

        # Report sections may embed another list builder through ``join``, for
        # example ``detail_lines -> report_sections -> analysis_report``. Follow
        # that ownership graph so repeated structured rows remain report output.
        changed = True
        while changed:
            changed = False
            for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
                if not (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"append", "extend"}
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in report_builders
                ):
                    continue
                for argument in call.args:
                    nested_builder = joined_builder_name(argument)
                    if (
                        not nested_builder
                        and call.func.attr == "extend"
                        and isinstance(argument, ast.Name)
                    ):
                        nested_builder = argument.id
                    if nested_builder and nested_builder not in report_builders:
                        report_builders.add(nested_builder)
                        changed = True
            for loop in (item for item in ast.walk(tree) if isinstance(item, ast.For)):
                if not isinstance(loop.iter, ast.Name):
                    continue
                source_builder = loop.iter.id
                target_names = set(_target_names(loop.target))
                if not target_names:
                    continue
                parent_builders: set[str] = set()
                for call in (item for item in ast.walk(loop) if isinstance(item, ast.Call)):
                    if not (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr == "append"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id in report_builders
                        and call.args
                    ):
                        continue
                    loaded = set(_loaded_names(call.args[0]))
                    if loaded.intersection(target_names):
                        parent_builders.add(call.func.value.id)
                for parent_builder in parent_builders:
                    link = (
                        parent_builder,
                        source_builder,
                        int(getattr(loop, "lineno", 0) or 0),
                        int(getattr(loop, "col_offset", 0) or 0),
                    )
                    if link not in iterated_builder_links:
                        iterated_builder_links.append(link)
                if parent_builders and source_builder not in report_builders:
                    report_builders.add(source_builder)
                    changed = True

        metadata: dict[str, dict[str, Any]] = {}
        synthetic_templates: list[tuple[ast.JoinedStr, dict[str, Any]]] = []

        def template_id(node: ast.JoinedStr) -> str:
            expressions = cls._joined_string_expressions(node, code)
            return str(expressions[0].get("template_id") or "") if expressions else ""

        for node in direct_templates:
            key = template_id(node)
            if key:
                metadata[key] = {"report_declared": True}
        synthetic_templates.extend(
            (node, {"report_declared": True})
            for node in direct_synthetic_templates
        )

        # A saved report commonly embeds prose fragments assembled earlier under
        # conditional branches, for example ``f"...{status_finding}..."``. Those
        # fragment f-strings remain part of the canonical report construction
        # graph even though they are not assigned to ``analysis_report`` directly.
        # Follow only named values referenced by an already-owned report template;
        # this keeps chart descriptions and unrelated diagnostic strings outside
        # the report evidence surface.
        def nested_string_templates(value: ast.AST | None) -> list[ast.JoinedStr]:
            """Find render templates beneath one named string expression."""
            if isinstance(value, ast.IfExp):
                return [
                    *nested_string_templates(value.body),
                    *nested_string_templates(value.orelse),
                ]
            if isinstance(value, ast.JoinedStr):
                return [value]
            if isinstance(value, ast.Call):
                formatted = cls._format_call_template(value)
                if formatted is not None:
                    return [formatted]
                if (
                    isinstance(value.func, ast.Attribute)
                    and value.func.attr == "join"
                    and value.args
                    and isinstance(
                        value.args[0],
                        (ast.ListComp, ast.GeneratorExp, ast.SetComp),
                    )
                    and isinstance(value.args[0].elt, ast.JoinedStr)
                ):
                    return [value.args[0].elt]
            if isinstance(value, ast.BinOp):
                concatenated = cls._concatenated_string_template(value)
                return [concatenated] if concatenated is not None else []
            return []

        named_string_templates: dict[str, list[ast.JoinedStr]] = {}
        for assignment in (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        ):
            value = getattr(assignment, "value", None)
            nested_templates = nested_string_templates(value)
            template = nested_templates[0] if nested_templates else None
            # Named detail fragments are frequently produced by joining a list
            # comprehension, for example ``"、".join(f"{name} {count}次" ...)``.
            # When that name is inserted into a report template, its element
            # f-string is part of the report construction graph as well.
            if (
                template is None
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "join"
                and value.args
                and isinstance(value.args[0], (ast.ListComp, ast.GeneratorExp, ast.SetComp))
                and isinstance(value.args[0].elt, ast.JoinedStr)
            ):
                template = value.args[0].elt
            if not isinstance(template, ast.JoinedStr):
                continue
            assignment_targets = (
                list(assignment.targets)
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            for name in (
                item.id
                for target in assignment_targets
                for item in ast.walk(target)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
            ):
                named_string_templates.setdefault(name, []).append(template)

        pending_templates = [*direct_templates, *direct_synthetic_templates]
        visited_fragment_names: set[str] = set()
        while pending_templates:
            owner = pending_templates.pop()
            fragment_names = {
                item.value.id
                for item in owner.values
                if isinstance(item, ast.FormattedValue)
                and isinstance(item.value, ast.Name)
                and item.value.id in named_string_templates
            }
            for fragment_name in fragment_names - visited_fragment_names:
                visited_fragment_names.add(fragment_name)
                for fragment_template in named_string_templates[fragment_name]:
                    key = template_id(fragment_template)
                    if key:
                        metadata[key] = {"report_declared": True}
                    pending_templates.append(fragment_template)

        entries: list[dict[str, Any]] = []

        def add_entry(
            builder: str,
            value: ast.AST,
            *,
            nested_builder: str = "",
        ) -> None:
            if isinstance(value, ast.IfExp):
                add_entry(builder, value.body)
                add_entry(builder, value.orelse)
                return
            entry = {
                "builder": builder,
                "line": int(getattr(value, "lineno", 0) or 0),
                "column": int(getattr(value, "col_offset", 0) or 0),
                "literal": "",
                "template": None,
                "nested_builder": nested_builder or joined_builder_name(value),
            }
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                entry["literal"] = value.value
            elif isinstance(value, ast.JoinedStr):
                entry["template"] = value
            elif isinstance(value, ast.Call):
                entry["template"] = cls._format_call_template(value)
            elif isinstance(value, ast.BinOp):
                entry["template"] = cls._concatenated_string_template(value)
            entries.append(entry)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = [name for name in _target_names(node) if name in report_builders]
            value = getattr(node, "value", None)
            if len(targets) != 1:
                continue
            if isinstance(value, (ast.List, ast.Tuple)):
                for item in value.elts:
                    add_entry(targets[0], item)
            elif isinstance(value, ast.ListComp):
                # A report builder often receives repeated rows through a list
                # comprehension and is later included via ``extend``. Treat its
                # element template as report-owned so every rendered row can be
                # located by the same AST calculation boundary.
                add_entry(targets[0], value.elt)

        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            if not (
                isinstance(call.func, ast.Attribute)
                and call.func.attr in {"append", "extend"}
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in report_builders
                and call.args
            ):
                continue
            value = call.args[0]
            if call.func.attr == "extend" and isinstance(value, (ast.List, ast.Tuple)):
                for item in value.elts:
                    add_entry(call.func.value.id, item)
            elif (
                call.func.attr == "extend"
                and isinstance(value, ast.Name)
                and value.id in report_builders
            ):
                add_entry(
                    call.func.value.id,
                    value,
                    nested_builder=value.id,
                )
            else:
                add_entry(call.func.value.id, value)
        for parent_builder, source_builder, line, column in iterated_builder_links:
            entries.append({
                "builder": parent_builder,
                "line": line,
                "column": column,
                "literal": "",
                "template": None,
                "nested_builder": source_builder,
            })
        entries.sort(key=lambda item: (item["line"], item["column"]))

        def surrounding_anchors(entry_index: int) -> tuple[list[str], list[str]]:
            entry = entries[entry_index]
            builder = entry.get("builder")
            before = [
                str(item.get("literal") or "")
                for item in entries[:entry_index]
                if item.get("builder") == builder and str(item.get("literal") or "")
            ]
            after = [
                str(item.get("literal") or "")
                for item in entries[entry_index + 1:]
                if item.get("builder") == builder and str(item.get("literal") or "")
            ]
            if before or after:
                return before, after
            if builder in builder_owner_anchors:
                return builder_owner_anchors[builder]
            parent_index = next(
                (
                    index for index, item in enumerate(entries)
                    if item.get("nested_builder") == builder
                ),
                -1,
            )
            return surrounding_anchors(parent_index) if parent_index >= 0 else ([], [])

        for index, entry in enumerate(entries):
            node = entry.get("template")
            if not isinstance(node, ast.JoinedStr):
                continue
            key = template_id(node)
            if not key:
                continue
            before, after = surrounding_anchors(index)
            metadata[key] = {
                "report_declared": True,
                "report_before_anchors": before,
                "report_after_anchors": after,
            }
            if node not in direct_templates and not any(
                isinstance(item, ast.JoinedStr) and item is node
                for item in ast.walk(tree)
            ):
                synthetic_templates.append((node, metadata[key]))

        # Conditional prose fragments referenced by a report-builder f-string
        # inherit report ownership just like fragments in a direct report string.
        pending_templates = [
            template
            for templates in named_string_templates.values()
            for template in templates
            if template_id(template) in metadata
        ] + [
            entry["template"]
            for entry in entries
            if isinstance(entry.get("template"), ast.JoinedStr)
            and template_id(entry["template"]) in metadata
        ]
        visited_fragment_owners: set[tuple[str, str]] = set()
        fragment_owner_dependencies: set[tuple[str, str]] = set()
        while pending_templates:
            owner = pending_templates.pop()
            owner_template_id = template_id(owner)
            owner_expressions = cls._joined_string_expressions(owner, code)

            def inherit_fragment_region(
                fragment_template: ast.JoinedStr,
                owner_slot: dict[str, Any],
            ) -> None:
                key = template_id(fragment_template)
                if not key:
                    return
                fragment_owner_dependencies.add((key, owner_template_id))
                owner_slot_index = int(owner_slot.get("slot_index") or 0)
                owner_tokens = list(owner_slot.get("template_tokens") or [])
                before_slot = True
                owner_before_literals: list[str] = []
                owner_after_literals: list[str] = []
                for token in owner_tokens:
                    if token.get("slot") == owner_slot_index:
                        before_slot = False
                        continue
                    literal = str(token.get("literal") or "")
                    if not literal.strip():
                        continue
                    if before_slot:
                        owner_before_literals.append(literal)
                    else:
                        owner_after_literals.append(literal)
                inherited = {
                    "report_declared": True,
                    "report_before_anchors": list(
                        metadata.get(owner_template_id, {}).get(
                            "report_before_anchors", []
                        )
                    ) + owner_before_literals,
                    "report_after_anchors": owner_after_literals + list(
                        metadata.get(owner_template_id, {}).get(
                            "report_after_anchors", []
                        )
                    ),
                }
                existing = metadata.get(key)
                existing_has_region = bool(
                    existing
                    and (
                        existing.get("report_before_anchors")
                        or existing.get("report_after_anchors")
                    )
                )
                if existing_has_region and (
                    existing.get("report_before_anchors")
                    != inherited["report_before_anchors"]
                    or existing.get("report_after_anchors")
                    != inherited["report_after_anchors"]
                ):
                    # A fragment rendered in several sections must rely on its
                    # own literals instead of one owner's positional region.
                    inherited["report_before_anchors"] = []
                    inherited["report_after_anchors"] = []
                metadata[key] = inherited
                if not any(
                    isinstance(item, ast.JoinedStr) and item is fragment_template
                    for item in ast.walk(tree)
                ) and not any(
                    existing_template is fragment_template
                    for existing_template, _ in synthetic_templates
                ):
                    synthetic_templates.append((fragment_template, inherited))

            fragment_names = {
                item.value.id
                for item in owner.values
                if isinstance(item, ast.FormattedValue)
                and isinstance(item.value, ast.Name)
                and item.value.id in named_string_templates
            }
            for fragment_name in fragment_names:
                owner_key = (fragment_name, owner_template_id)
                if owner_key in visited_fragment_owners:
                    continue
                visited_fragment_owners.add(owner_key)
                for fragment_template in named_string_templates[fragment_name]:
                    owner_slot = next(
                        (
                            expression
                            for expression in owner_expressions
                            if str(expression.get("expression") or "").strip()
                            == fragment_name
                        ),
                        {},
                    )
                    inherit_fragment_region(fragment_template, owner_slot)
                    pending_templates.append(fragment_template)

            formatted_values = [
                item for item in owner.values
                if isinstance(item, ast.FormattedValue)
            ]
            for slot_index, formatted_value in enumerate(formatted_values):
                if not isinstance(formatted_value.value, ast.IfExp):
                    continue
                inline_key = (
                    f"inline:{getattr(formatted_value.value, 'lineno', 0)}:"
                    f"{getattr(formatted_value.value, 'col_offset', 0)}"
                )
                owner_key = (inline_key, owner_template_id)
                if owner_key in visited_fragment_owners:
                    continue
                visited_fragment_owners.add(owner_key)
                owner_slot = next(
                    (
                        expression for expression in owner_expressions
                        if int(expression.get("slot_index") or 0) == slot_index
                    ),
                    {},
                )
                for fragment_template in nested_string_templates(
                    formatted_value.value
                ):
                    inherit_fragment_region(fragment_template, owner_slot)
                    pending_templates.append(fragment_template)
        changed = True
        while changed:
            changed = False
            for child_id, owner_id in fragment_owner_dependencies:
                owner_metadata = metadata.get(owner_id) or {}
                child_metadata = metadata.get(child_id) or {}
                if (
                    owner_metadata.get("report_before_anchors")
                    or owner_metadata.get("report_after_anchors")
                    or not (
                        child_metadata.get("report_before_anchors")
                        or child_metadata.get("report_after_anchors")
                    )
                ):
                    continue
                child_metadata["report_before_anchors"] = []
                child_metadata["report_after_anchors"] = []
                metadata[child_id] = child_metadata
                changed = True
        synthetic_templates = [
            (template, metadata.get(template_id(template), template_metadata))
            for template, template_metadata in synthetic_templates
        ]
        return metadata, synthetic_templates

    @staticmethod
    def _format_call_template(node: ast.Call) -> ast.JoinedStr | None:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant)
            and isinstance(node.func.value.value, str)
        ):
            return None
        values: list[ast.AST] = []
        auto_index = 0
        keyword_values = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        try:
            parsed = list(string.Formatter().parse(node.func.value.value))
        except ValueError:
            return None
        for literal, field_name, format_spec, conversion in parsed:
            if literal:
                values.append(ast.copy_location(ast.Constant(value=literal), node))
            if field_name is None:
                continue
            if field_name == "":
                argument_index = auto_index
                auto_index += 1
                if argument_index >= len(node.args):
                    return None
                expression = node.args[argument_index]
            elif field_name.isdigit():
                argument_index = int(field_name)
                if argument_index >= len(node.args):
                    return None
                expression = node.args[argument_index]
            elif field_name in keyword_values:
                expression = keyword_values[field_name]
            else:
                return None
            format_node = (
                ast.copy_location(
                    ast.JoinedStr(values=[ast.Constant(value=format_spec)]),
                    node,
                )
                if format_spec
                else None
            )
            conversion_code = ord(conversion) if conversion in {"s", "r", "a"} else -1
            values.append(ast.copy_location(
                ast.FormattedValue(
                    value=expression,
                    conversion=conversion_code,
                    format_spec=format_node,
                ),
                node,
            ))
        return ast.copy_location(ast.JoinedStr(values=values), node) if values else None

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

    @classmethod
    def _mapping_literal_member_operations(
        cls,
        node: ast.AST,
        code: str,
    ) -> list[dict[str, Any]]:
        """Index each calculated member of a business-labelled mapping.

        A common analytical shape builds ``{"category": expression}``, sorts
        ``mapping.items()``, and renders every value through one loop variable.
        Treating the whole mapping as one operation collapses independent metric
        boundaries. Static labels let us preserve each branch without relying
        on the eventual numeric value.
        """
        value = getattr(node, "value", None)
        targets = _target_names(node)
        if len(targets) != 1 or not isinstance(value, ast.Dict):
            return []
        container_name = targets[0]
        container_path: list[str] = []
        try:
            container_node = ast.parse(container_name, mode="eval").body
            while isinstance(container_node, ast.Subscript):
                selector = (
                    container_node.slice.value
                    if isinstance(container_node.slice, ast.Constant)
                    and isinstance(container_node.slice.value, str)
                    else None
                )
                if selector is None:
                    container_path = []
                    break
                container_path.insert(0, selector)
                container_node = container_node.value
        except SyntaxError:
            container_path = []
        operations: list[dict[str, Any]] = []
        for key_node, expression in zip(value.keys, value.values):
            if not (
                isinstance(key_node, ast.Constant)
                and isinstance(key_node.value, str)
            ):
                continue
            loads = _loaded_names(expression)
            called = _called_operation_names(expression)
            has_formula = any(
                isinstance(item, (ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp))
                for item in ast.walk(expression)
            )
            if not loads or not (
                has_formula
                or called
                or any(isinstance(item, ast.Subscript) for item in ast.walk(expression))
            ):
                continue
            if called.intersection({
                "sum", "mean", "count", "nunique", "min", "max", "len",
                "size", "agg", "aggregate", "value_counts", "quantile",
            }):
                semantic_kind = "aggregation"
            elif has_formula:
                semantic_kind = "formula"
            else:
                semantic_kind = "projection"
            expression_source = (
                ast.get_source_segment(code, expression) or ast.unparse(expression)
            )
            target = f"{container_name}[{key_node.value!r}]"
            source = f"{target} = {expression_source}"
            mapping_path = [*container_path, key_node.value]
            operations.append({
                "operation_node_id": (
                    f"op_mapping_member_{getattr(expression, 'lineno', 0)}_"
                    f"{_digest(source, 10)}"
                ),
                "line": int(getattr(expression, "lineno", 0) or 0),
                "kind": "calculation",
                "semantic_kind": semantic_kind,
                "query_node_id": "",
                "targets": [target],
                "loads": loads,
                "source": source[:1200],
                "parameter_bindings": {
                    "mapping_key": key_node.value,
                    "mapping_path": mapping_path,
                    "mapping_selector": (
                        container_path[-1] if container_path else key_node.value
                    ),
                },
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
                # Column aliases describe pure projection/renaming only.  A
                # formula may reference several source columns, but mapping it
                # to the first one would collapse a distinct metric boundary.
                projection = value
                while (
                    isinstance(projection, ast.Call)
                    and isinstance(projection.func, ast.Attribute)
                    and projection.func.attr in {
                        "astype", "fillna", "round", "replace", "clip",
                    }
                ):
                    projection = projection.func.value
                if (
                    isinstance(projection, ast.Subscript)
                    and isinstance(projection.value, ast.Name)
                    and projection.value.id == frame_name
                    and isinstance(projection.slice, ast.Constant)
                    and isinstance(projection.slice.value, str)
                ):
                    aliases.setdefault(frame_name, {})[
                        str(target.slice.value)
                    ] = str(projection.slice.value)
            if len(targets) == 1 and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr == "rename":
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
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg in {"rows", "data"}
                ),
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
            source_frame = LineageIndexer._record_iterator_source_frame(
                generator.iter
            )
            if source_frame:
                return source_frame
        return ""

    @staticmethod
    def _record_iterator_source_frame(iterator: ast.AST | None) -> str:
        """Resolve a row iterator to its originating DataFrame expression."""
        if (
            isinstance(iterator, ast.Call)
            and isinstance(iterator.func, ast.Name)
            and iterator.func.id == "enumerate"
            and iterator.args
        ):
            iterator = iterator.args[0]
        for call in ast.walk(iterator) if iterator is not None else []:
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr in {"iterrows", "itertuples"}:
                return LineageIndexer._expression_base_frame(call.func.value)
            if call.func.attr == "groupby":
                # Iterating a pandas GroupBy yields ``(group_key,
                # group_frame)``. The group frame keeps the source DataFrame's
                # column provenance even when one row is later selected and
                # accumulated into a new DataFrame.
                return LineageIndexer._expression_base_frame(call.func.value)
            if call.func.attr != "to_dict":
                continue
            positional_orient = (
                str(call.args[0].value).strip().lower()
                if call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                else ""
            )
            keyword_orient = next(
                (
                    str(keyword.value.value).strip().lower()
                    for keyword in call.keywords
                    if keyword.arg == "orient"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                "",
            )
            if (positional_orient or keyword_orient) == "records":
                return LineageIndexer._expression_base_frame(call.func.value)
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
    def _chart_data_sources(tree: ast.AST) -> list[dict[str, Any]]:
        assignments: dict[str, ast.AST] = {}
        for assignment in sorted(
            (
                item for item in ast.walk(tree)
                if isinstance(item, (ast.Assign, ast.AnnAssign))
            ),
            key=lambda item: int(getattr(item, "lineno", 0) or 0),
        ):
            value = getattr(assignment, "value", None)
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    assignments[target.id] = value

        def resolve(expression: ast.AST | None) -> ast.AST | None:
            seen: set[str] = set()
            while (
                isinstance(expression, ast.Name)
                and expression.id in assignments
                and expression.id not in seen
            ):
                seen.add(expression.id)
                expression = assignments[expression.id]
            return expression

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
        sources: list[dict[str, Any]] = []
        for call in calls:
            data_expression = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "data"),
                call.args[0] if call.args else None,
            )
            source_frame = LineageIndexer._expression_base_frame(data_expression)
            function_name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else ""
            )

            def constant_keyword(name: str) -> str:
                value = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == name),
                    None,
                )
                return (
                    str(value.value)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else ""
                )

            value_fields: list[str] = []
            value_field = constant_keyword("value_field")
            if value_field:
                value_fields.append(value_field)
            value_fields_node = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "value_fields"),
                None,
            )
            if isinstance(value_fields_node, (ast.List, ast.Tuple)):
                value_fields.extend(
                    str(item.value)
                    for item in value_fields_node.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
            sources.append({
                "source_frame": source_frame,
                "title": constant_keyword("title"),
                "chart_kind": constant_keyword("chart_kind"),
                "category_field": constant_keyword("category_field"),
                "value_fields": list(dict.fromkeys(value_fields)),
                "column_expressions": LineageIndexer._literal_frame_column_expressions(
                    resolve(data_expression) or data_expression
                ),
                "reusable": function_name == "build_chart_suite",
            })
        return sources

    @staticmethod
    def _table_artifact_expressions(tree: ast.AST) -> list[ast.AST]:
        """Return table artifact expressions in the order passed to save_analysis_result.

        Generated code may use the helper, a direct table dict, or append/extend a
        prebuilt value. They are equivalent runtime forms and must share one lineage
        contract instead of falling back to column-name matching.
        """
        bindings: dict[str, ast.AST] = {}
        for node in sorted(
            (item for item in ast.walk(tree) if isinstance(item, (ast.Assign, ast.AnnAssign))),
            key=lambda item: int(getattr(item, "lineno", 0) or 0),
        ):
            targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            value = getattr(node, "value", None)
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    bindings[target.id] = value

        def resolve(expression: ast.AST) -> ast.AST:
            seen: set[str] = set()
            while isinstance(expression, ast.Name) and expression.id in bindings and expression.id not in seen:
                seen.add(expression.id)
                expression = bindings[expression.id]
            if (
                isinstance(expression, ast.Subscript)
                and isinstance(expression.value, ast.Name)
                and expression.value.id in bindings
                and isinstance(expression.slice, ast.Constant)
                and expression.slice.value == "table"
            ):
                # ``build_table_result`` returns a wrapper containing the actual
                # table artifact.  Generated code commonly stores that wrapper
                # before appending ``wrapper["table"]``.  For lineage purposes
                # the wrapper extraction and the helper call are the same table
                # declaration, so retain the helper's data argument.
                return resolve(bindings[expression.value.id])
            return expression

        events: list[tuple[int, int, ast.AST]] = []
        sequence = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
                if not any(isinstance(target, ast.Name) and target.id == "tables" for target in targets):
                    continue
                value = getattr(node, "value", None)
                if isinstance(value, (ast.List, ast.Tuple)):
                    for item in value.elts:
                        events.append((int(getattr(node, "lineno", 0) or 0), sequence, resolve(item)))
                        sequence += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "tables"
                and node.args
            ):
                if node.func.attr == "append":
                    events.append((int(getattr(node, "lineno", 0) or 0), sequence, resolve(node.args[0])))
                    sequence += 1
                elif node.func.attr == "extend" and isinstance(resolve(node.args[0]), (ast.List, ast.Tuple)):
                    for item in resolve(node.args[0]).elts:
                        events.append((int(getattr(node, "lineno", 0) or 0), sequence, resolve(item)))
                        sequence += 1
            elif (
                isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "save_analysis_result")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "save_analysis_result"
                    )
                )
            ):
                tables_value = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "tables"),
                    None,
                )
                resolved_tables = resolve(tables_value) if tables_value is not None else None
                if isinstance(resolved_tables, (ast.List, ast.Tuple)):
                    for item in resolved_tables.elts:
                        events.append((
                            int(getattr(node, "lineno", 0) or 0),
                            sequence,
                            resolve(item),
                        ))
                        sequence += 1
        expressions: list[ast.AST] = []
        seen: set[tuple[int, int, str]] = set()
        for _, _, expression in sorted(events, key=lambda item: (item[0], item[1])):
            identity = (
                int(getattr(expression, "lineno", 0) or 0),
                int(getattr(expression, "col_offset", 0) or 0),
                ast.dump(expression, include_attributes=False),
            )
            if identity in seen:
                continue
            seen.add(identity)
            expressions.append(expression)
        return expressions

    @staticmethod
    def _dict_value(expression: ast.AST, key: str) -> ast.AST | None:
        if not isinstance(expression, ast.Dict):
            return None
        for key_node, value_node in zip(expression.keys, expression.values):
            if isinstance(key_node, ast.Constant) and key_node.value == key:
                return value_node
        return None

    @classmethod
    def _table_rows_expression(cls, expression: ast.AST) -> ast.AST | None:
        if isinstance(expression, ast.Call) and (
            (isinstance(expression.func, ast.Name) and expression.func.id == "build_table_result")
            or (isinstance(expression.func, ast.Attribute) and expression.func.attr == "build_table_result")
        ):
            return next(
                (
                    keyword.value
                    for keyword in expression.keywords
                    if keyword.arg in {"rows", "data"}
                ),
                None,
            )
        if isinstance(expression, ast.Dict):
            nested = cls._dict_value(expression, "table")
            if isinstance(nested, ast.Dict):
                expression = nested
            return cls._dict_value(expression, "rows")
        return None

    @staticmethod
    def _expression_base_frame(expression: ast.AST | None) -> str:
        if isinstance(expression, ast.Name):
            return expression.id
        if isinstance(expression, ast.IfExp):
            branch_frames = {
                frame
                for frame in (
                    LineageIndexer._expression_base_frame(expression.body),
                    LineageIndexer._expression_base_frame(expression.orelse),
                )
                if frame
            }
            return next(iter(branch_frames)) if len(branch_frames) == 1 else ""
        if isinstance(expression, ast.Call):
            function_name = (
                expression.func.id
                if isinstance(expression.func, ast.Name)
                else expression.func.attr
                if isinstance(expression.func, ast.Attribute)
                else ""
            )
            if function_name == "DataFrame":
                source = expression.args[0] if expression.args else None
                if isinstance(source, ast.Name):
                    return source.id
                if isinstance(source, (ast.Call, ast.Attribute, ast.Subscript)):
                    return LineageIndexer._expression_base_frame(source)
                return ""
            if isinstance(expression.func, ast.Attribute):
                return LineageIndexer._expression_base_frame(expression.func.value)
            return ""
        if isinstance(expression, (ast.Attribute, ast.Subscript)):
            return LineageIndexer._expression_base_frame(expression.value)
        return ""

    @staticmethod
    def _literal_frame_column_expressions(
        expression: ast.AST | None,
    ) -> dict[str, list[ast.AST]]:
        """Return the value expressions owned by each inline frame column."""
        if isinstance(expression, ast.Call):
            function_name = (
                expression.func.id
                if isinstance(expression.func, ast.Name)
                else expression.func.attr
                if isinstance(expression.func, ast.Attribute)
                else ""
            )
            if function_name != "DataFrame" or not expression.args:
                return {}
            expression = expression.args[0]

        records = (
            [expression]
            if isinstance(expression, ast.Dict)
            else list(expression.elts)
            if isinstance(expression, (ast.List, ast.Tuple))
            and all(isinstance(item, ast.Dict) for item in expression.elts)
            else []
        )
        columns: dict[str, list[ast.AST]] = {}
        for record in records:
            if not isinstance(record, ast.Dict):
                continue
            for key_node, value_node in zip(record.keys, record.values):
                if not (
                    isinstance(key_node, ast.Constant)
                    and isinstance(key_node.value, str)
                    and str(key_node.value).strip()
                ):
                    continue
                values = (
                    list(value_node.elts)
                    if isinstance(value_node, (ast.List, ast.Tuple))
                    else [value_node]
                )
                columns.setdefault(str(key_node.value), []).extend(values)
        return columns

    @classmethod
    def _table_data_sources(cls, tree: ast.AST) -> list[dict[str, Any]]:
        loop_ranges = [
            (
                int(getattr(item, "lineno", 0) or 0),
                int(getattr(item, "end_lineno", 0) or 0),
            )
            for item in ast.walk(tree)
            if isinstance(item, (ast.For, ast.AsyncFor, ast.While))
        ]
        wrapper_contracts: dict[str, dict[str, Any]] = {}
        for function in (
            item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef)
        ):
            parameter_names = [item.arg for item in function.args.args]
            for call in (
                item for item in ast.walk(function) if isinstance(item, ast.Call)
            ):
                function_name = (
                    call.func.id
                    if isinstance(call.func, ast.Name)
                    else call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else ""
                )
                if function_name != "build_table_result":
                    continue
                data_expression = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg in {"rows", "data"}
                    ),
                    call.args[0] if call.args else None,
                )
                if not (
                    isinstance(data_expression, ast.Name)
                    and data_expression.id in parameter_names
                ):
                    continue
                wrapper_contracts[function.name] = {
                    "data_parameter": data_expression.id,
                    "data_parameter_index": parameter_names.index(data_expression.id),
                }
                break

        sources: list[dict[str, Any]] = []
        for expression in cls._table_artifact_expressions(tree):
            rows_expression = cls._table_rows_expression(expression)
            if (
                rows_expression is None
                and isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id in wrapper_contracts
            ):
                contract = wrapper_contracts[expression.func.id]
                parameter_name = str(contract["data_parameter"])
                parameter_index = int(contract["data_parameter_index"])
                rows_expression = next(
                    (
                        keyword.value
                        for keyword in expression.keywords
                        if keyword.arg == parameter_name
                    ),
                    expression.args[parameter_index]
                    if parameter_index < len(expression.args)
                    else None,
                )
            source = ""
            column_expressions = cls._literal_frame_column_expressions(
                rows_expression
            )
            if isinstance(rows_expression, ast.ListComp):
                source = cls._list_comprehension_source_frame(rows_expression)
            elif isinstance(rows_expression, ast.Call):
                source = cls._expression_base_frame(rows_expression)
            if (
                not source
                and not column_expressions
                and not isinstance(rows_expression, (ast.List, ast.Tuple, ast.Dict))
            ):
                loaded = _loaded_names(rows_expression) if rows_expression is not None else []
                source = loaded[0] if loaded else ""
            title = ""
            if isinstance(expression, ast.Call):
                title_node = next(
                    (keyword.value for keyword in expression.keywords if keyword.arg == "title"),
                    None,
                )
                if isinstance(title_node, ast.Constant) and isinstance(title_node.value, str):
                    title = str(title_node.value)
            expression_line = int(getattr(expression, "lineno", 0) or 0)
            sources.append({
                "source_frame": source,
                "title": title,
                "column_expressions": column_expressions,
                "reusable": any(
                    start <= expression_line <= end
                    for start, end in loop_ranges
                    if start and end
                ),
            })
        return sources

    @classmethod
    def _artifact_datasource_query_node_id(
        cls,
        snapshot: AnalysisExecutionSnapshot,
        artifact_title: str,
    ) -> str:
        """Resolve a runtime artifact to one successful physical query.

        A single chart/table declaration can execute once per datasource in a
        loop.  The rendered artifact title then carries the runtime datasource
        identity even though the static source expression is shared.  Match
        that identity to the selected datasource and then to the successful
        query implementation.  Ambiguous matches deliberately remain empty.
        """
        normalized_title = str(artifact_title or "").strip().casefold()
        if not normalized_title:
            return ""

        datasource_matches: list[dict[str, Any]] = []
        for datasource in snapshot.selected_datasource_snapshot:
            if not isinstance(datasource, dict):
                continue
            labels = list(dict.fromkeys(
                str(datasource.get(key) or "").strip()
                for key in (
                    "datasource_name", "display_name", "displayName", "name",
                )
                if str(datasource.get(key) or "").strip()
            ))
            if any(
                normalized_title == label.casefold()
                or bool(re.match(
                    rf"^{re.escape(label.casefold())}(?:\s|[-_:/（(【[])",
                    normalized_title,
                ))
                for label in labels
            ):
                datasource_matches.append(datasource)
        if len(datasource_matches) != 1:
            return ""

        datasource = datasource_matches[0]
        identifiers = list(dict.fromkeys(
            str(datasource.get(key) or "").strip()
            for key in ("datasource_identifier", "identifier", "alias")
            if str(datasource.get(key) or "").strip()
        ))
        if not identifiers:
            return ""

        query_matches: list[str] = []
        for query_id, raw_snapshot in cls._lineage_query_snapshots(
            snapshot.query_snapshots
        ).items():
            query_snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
            implementation = (
                query_snapshot.get("implementation")
                if isinstance(query_snapshot.get("implementation"), dict)
                else {}
            )
            implementation_text = _stable_json(implementation).casefold()
            identifier_tokens = list(dict.fromkeys(
                token
                for identifier in identifiers
                for token in (
                    identifier.casefold(),
                    identifier.rsplit(".", 1)[-1].casefold(),
                )
                if token
            ))
            if any(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
                    implementation_text,
                )
                for token in identifier_tokens
            ):
                query_matches.append(str(query_id))
        return query_matches[0] if len(query_matches) == 1 else ""

    @classmethod
    def _attach_runtime_output_query_identity(
        cls,
        outputs: list[dict[str, Any]],
        snapshot: AnalysisExecutionSnapshot,
    ) -> list[dict[str, Any]]:
        """Attach query identity carried by the rendered runtime artifact.

        Static AST lineage cannot distinguish iterations of one shared loop
        variable.  A rendered chart/table title or report section heading can,
        provided it uniquely names one selected datasource and that datasource
        uniquely appears in one successful physical query.
        """
        enriched: list[dict[str, Any]] = []
        for raw_output in outputs:
            output = dict(raw_output)
            if str(output.get("source_query_node_id") or "").strip():
                enriched.append(output)
                continue
            artifact_type = str(output.get("artifact_type") or "")
            artifact_index = int(output.get("artifact_index") or 0)
            title = ""
            if artifact_type == "report":
                title = str(
                    (output.get("display_context") or {}).get("heading") or ""
                ).strip()
            elif artifact_type == "chart" and 0 <= artifact_index < len(snapshot.charts):
                title = str(snapshot.charts[artifact_index].get("title") or "").strip()
            elif artifact_type == "table" and 0 <= artifact_index < len(snapshot.tables):
                title = str(snapshot.tables[artifact_index].get("title") or "").strip()
            query_node_id = cls._artifact_datasource_query_node_id(snapshot, title)
            if query_node_id:
                output["source_query_node_id"] = query_node_id
                output["runtime_artifact_query_identity"] = True
            expression_query_ids = cls._report_expression_query_node_ids(
                snapshot,
                output,
            )
            if expression_query_ids:
                output["runtime_expression_query_node_ids"] = expression_query_ids
            enriched.append(output)
        return enriched

    @classmethod
    def _report_expression_query_node_ids(
        cls,
        snapshot: AnalysisExecutionSnapshot,
        output: dict[str, Any],
    ) -> list[str]:
        """Resolve symbolic runtime selectors used by a report expression.

        For example, ``m1 -> df1 -> all_results[names[0]]`` cannot be linked
        to a physical query by static variable names alone.  The rendered
        f-string bindings retain the concrete value of ``names[0]``; combining
        that value with the selected datasource/query identity yields a
        deterministic query boundary for ``m1`` without interpreting prose.
        """
        if output.get("artifact_type") != "report":
            return []
        expression_text = str(output.get("field") or "").strip()
        bindings = output.get("rendered_slot_bindings") or {}
        if not expression_text or not isinstance(bindings, dict) or not bindings:
            return []
        try:
            code_tree = ast.parse(snapshot.generated_code or "")
            expression_tree = ast.parse(expression_text, mode="eval")
        except SyntaxError:
            return []

        assignments: dict[str, list[tuple[int, ast.AST]]] = {}
        for node in ast.walk(code_tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = getattr(node, "value", None)
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append((
                        int(getattr(node, "lineno", 0) or 0),
                        value,
                    ))
        for values in assignments.values():
            values.sort(key=lambda item: item[0])

        source_line = int(output.get("source_line") or 0) or 10**9
        query_ids: list[str] = []
        visited: set[tuple[str, int]] = set()

        def visit(node: ast.AST, before_line: int) -> None:
            for subscript in (
                item for item in ast.walk(node) if isinstance(item, ast.Subscript)
            ):
                try:
                    selector = ast.unparse(subscript.slice).strip()
                except Exception:
                    selector = ""
                rendered_datasource = str(bindings.get(selector) or "").strip()
                if not rendered_datasource:
                    continue
                query_id = cls._artifact_datasource_query_node_id(
                    snapshot,
                    rendered_datasource,
                )
                if query_id:
                    query_ids.append(query_id)
            loaded_names = {
                item.id
                for item in ast.walk(node)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
            }
            for name in loaded_names:
                assignment = next(
                    (
                        (line, value)
                        for line, value in reversed(assignments.get(name, []))
                        if line <= before_line
                    ),
                    None,
                )
                if assignment is None or (name, assignment[0]) in visited:
                    continue
                visited.add((name, assignment[0]))
                visit(assignment[1], assignment[0])

        visit(expression_tree, source_line)
        return list(dict.fromkeys(query_ids))

    @classmethod
    def _table_row_datasource_query_node_ids(
        cls,
        snapshot: AnalysisExecutionSnapshot,
        table: dict[str, Any],
    ) -> list[str]:
        """Return physical queries named by a table's datasource dimension.

        Multi-source summaries commonly append one record per datasource into
        a shared table.  The metric Evidence remains column-scoped, while the
        datasource-name column provides deterministic runtime provenance for
        every row contributing to that column.
        """
        rows = [
            row for row in table.get("rows") or []
            if isinstance(row, dict)
        ]
        if not rows:
            return []
        candidate_fields = set(rows[0])
        for row in rows[1:]:
            candidate_fields.intersection_update(row)
        for field in candidate_fields:
            values = [str(row.get(field) or "").strip() for row in rows]
            if not all(values):
                continue
            query_ids = [
                cls._artifact_datasource_query_node_id(snapshot, value)
                for value in values
            ]
            if all(query_ids):
                return list(dict.fromkeys(query_ids))
        return []

    @classmethod
    def _table_structured_metric_fields(cls, tree: ast.AST) -> list[set[str]]:
        result: list[set[str]] = []
        for expression in cls._table_artifact_expressions(tree):
            rows_expression = cls._table_rows_expression(expression)
            metric_fields: set[str] = set()
            if isinstance(rows_expression, ast.ListComp) and isinstance(rows_expression.elt, ast.Dict):
                row_names = cls._list_comprehension_row_names(rows_expression)
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
        if any(isinstance(item, ast.BinOp) for item in ast.walk(tree)):
            return "formula"
        if called.intersection({"len", "size"}):
            return "aggregation"
        return "presentation"

    @classmethod
    def _operation_semantic_kind(cls, node: ast.AST, *, query_node_id: str = "") -> str:
        if query_node_id:
            return "query_load"
        called = _called_operation_names(node)
        loaded_names = set(_loaded_names(node))
        presentation_parameter_names = {
            "analysis_parameters", "int", "float", "str", "bool", "round",
        }
        if (
            "analysis_parameters" in loaded_names
            and not (loaded_names - presentation_parameter_names)
        ):
            return "presentation"
        if any(isinstance(item, ast.BinOp) for item in ast.walk(node)):
            return "formula"
        if called.intersection({
            "sum", "mean", "count", "nunique", "min", "max", "len", "size",
            "agg", "aggregate", "value_counts", "quantile", "describe", "cumsum", "cumcount",
        }):
            return "aggregation"
        if called.intersection({"sort_values", "head", "tail", "rank", "idxmin", "idxmax"}):
            return "ranking"
        if cls._contains_relational_join(node):
            return "join"
        if called.intersection({"groupby", "pivot", "pivot_table", "resample", "rolling"}):
            return "grouping"
        value = getattr(node, "value", None)
        if (
            any(isinstance(item, ast.Subscript) for item in ast.walk(value))
            and (
                isinstance(value, ast.IfExp)
                or called.intersection({"abs", "float", "int", "round"})
            )
        ):
            # Selecting one scalar from an already aggregated frame establishes
            # a narrower calculation boundary than the frame's source dimension.
            # Treat the conditional/coerced selection as an explicit projection
            # so its report slot does not remain ambiguously bound to both.
            return "projection"
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
            if function_name in {
                "query_data_with_sql",
                "query_local_file",
                "query_local_file_low_memory",
            }:
                query_key_node = keywords.get("query_key")
                if (
                    isinstance(query_key_node, ast.Constant)
                    and isinstance(query_key_node.value, str)
                    and str(query_key_node.value).strip() in query_snapshots
                ):
                    bindings[ast.dump(node, include_attributes=False)] = str(
                        query_key_node.value
                    ).strip()
                continue
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
            if not query_key and isinstance(resolved_expression, ast.JoinedStr):
                matching_keys = [
                    str(key)
                    for key, snapshot in query_snapshots.items()
                    if isinstance(snapshot, dict)
                    and LineageIndexer._joined_sql_matches(
                        resolved_expression,
                        str((snapshot.get("implementation") or {}).get("sql") or ""),
                        sql_truncated=bool(
                            (snapshot.get("implementation") or {}).get("sql_truncated")
                        ),
                    )
                ]
                if len(matching_keys) == 1:
                    query_key = matching_keys[0]
            if query_key:
                bindings[ast.dump(sql_expression, include_attributes=False)] = query_key
        return bindings

    @staticmethod
    def _joined_sql_matches(
        expression: ast.JoinedStr,
        sql: str,
        *,
        sql_truncated: bool = False,
    ) -> bool:
        if not str(sql or "").strip():
            return False
        if sql_truncated:
            leading_literal = next(
                (
                    str(item.value)
                    for item in expression.values
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and str(item.value).strip()
                ),
                "",
            )
            # Truncated snapshots cannot satisfy the complete f-string pattern.
            # A substantial exact prefix is safe because the caller accepts the
            # binding only when exactly one executed query matches it.
            if len(leading_literal.strip()) >= 32:
                return sql.lstrip().startswith(leading_literal.lstrip())
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
                "query_data_with_sql", "query_local_file",
                "query_local_file_low_memory",
            }:
                continue
            direct_query_key = query_bindings.get(
                ast.dump(call, include_attributes=False)
            )
            if direct_query_key:
                return direct_query_key
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

    @staticmethod
    def _numeric_call_name(node: ast.AST) -> str:
        if not isinstance(node, ast.Call):
            return ""
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    @classmethod
    def _is_numeric_report_expression(
        cls,
        node: ast.AST | None,
        formatted_numeric_names: dict[str, str],
    ) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Constant):
            return isinstance(node.value, (int, float)) and not isinstance(
                node.value, bool
            )
        if isinstance(node, ast.Name):
            return node.id in formatted_numeric_names
        if isinstance(node, (ast.BinOp, ast.UnaryOp)):
            return True
        if isinstance(node, ast.IfExp):
            return cls._is_numeric_report_expression(
                node.body, formatted_numeric_names
            ) and cls._is_numeric_report_expression(
                node.orelse, formatted_numeric_names
            )
        if isinstance(node, ast.Call):
            return cls._numeric_call_name(node) in {
                "abs", "count", "float", "int", "len", "max", "mean",
                "min", "nunique", "round", "size", "sum",
            }
        return False

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
            if len(formatted) != 1:
                continue
            format_specs = [cls._format_spec_text(item.format_spec) for item in formatted]
            if not any(re.search(r"[bcdoxXneEfFgG%]", spec) for spec in format_specs):
                continue
            literal_text = "".join(
                str(item.value)
                for item in value.values
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ).strip()
            if literal_text and not re.fullmatch(r"[%$¥￥,+\-\s]*", literal_text):
                continue
            suffix = "%" if any("%" in spec for spec in format_specs) else ""
            raw_targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            for target in raw_targets:
                if isinstance(target, ast.Name):
                    names[target.id] = suffix

        numeric_calls = {
            "abs", "count", "float", "int", "len", "max", "mean", "min",
            "nunique", "round", "size", "sum",
        }

        def is_numeric_expression(value: ast.AST | None) -> bool:
            if value is None:
                return False
            if isinstance(value, ast.Constant):
                return isinstance(value.value, (int, float)) and not isinstance(value.value, bool)
            if isinstance(value, ast.Name):
                return value.id in names
            if isinstance(value, (ast.BinOp, ast.UnaryOp)):
                return True
            if isinstance(value, ast.IfExp):
                return is_numeric_expression(value.body) and is_numeric_expression(value.orelse)
            if isinstance(value, ast.Call):
                function_name = ""
                if isinstance(value.func, ast.Name):
                    function_name = value.func.id
                elif isinstance(value.func, ast.Attribute):
                    function_name = value.func.attr
                return function_name in numeric_calls
            return False

        assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ]
        changed = True
        while changed:
            changed = False
            for node in assignments:
                value = getattr(node, "value", None)
                if not is_numeric_expression(value):
                    continue
                raw_targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
                for target in raw_targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names[target.id] = ""
                        changed = True
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
                preformatted_name = (
                    value.value.id in formatted_numeric_names
                    if isinstance(value.value, ast.Name)
                    else False
                )
                preformatted_suffix = (
                    formatted_numeric_names.get(value.value.id, "")
                    if preformatted_name and isinstance(value.value, ast.Name)
                    else ""
                )
                numeric_hint = (
                    bool(format_spec)
                    or cls._is_numeric_report_expression(
                        value.value,
                        formatted_numeric_names,
                    )
                    or preformatted_name
                )
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
                "numeric_suffix": str(slot_token.get("numeric_suffix") or ""),
                "presentation_ordinal": _is_markdown_ordinal_slot(
                    prefix_text,
                    suffix_text,
                ),
            })
            slot_index += 1
        return expressions

    @staticmethod
    def _flatten_string_concatenation(node: ast.AST) -> list[ast.AST]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return [
                *LineageIndexer._flatten_string_concatenation(node.left),
                *LineageIndexer._flatten_string_concatenation(node.right),
            ]
        return [node]

    @classmethod
    def _concatenated_string_template(cls, node: ast.AST) -> ast.JoinedStr | None:
        parts = cls._flatten_string_concatenation(node)
        if len(parts) < 2 or not any(
            isinstance(part, ast.JoinedStr)
            or (isinstance(part, ast.Constant) and isinstance(part.value, str))
            for part in parts
        ):
            return None
        values: list[ast.AST] = []
        for part in parts:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                values.append(ast.copy_location(ast.Constant(value=part.value), part))
                continue
            if isinstance(part, ast.JoinedStr):
                values.extend(part.values)
                continue
            expression = part
            if (
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Name)
                and part.func.id == "str"
                and len(part.args) == 1
                and not part.keywords
            ):
                expression = part.args[0]
            values.append(ast.copy_location(
                ast.FormattedValue(value=expression, conversion=-1),
                part,
            ))
        return ast.copy_location(ast.JoinedStr(values=values), node)

    def _index_outputs(
        self,
        snapshot: AnalysisExecutionSnapshot,
        report_expressions: list[dict[str, Any]],
        *,
        column_aliases: dict[str, dict[str, str]],
        chart_sources: list[dict[str, Any]],
        table_sources: list[dict[str, Any]],
        table_metric_fields: list[set[str]],
    ) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        matched_chart_contracts: set[int] = set()
        for chart_index, chart in enumerate(snapshot.charts):
            rows = _chart_rows(chart)
            data_contract = (
                chart.get("chart_data_contract")
                if isinstance(chart.get("chart_data_contract"), dict)
                else {}
            )
            source_query_node_id = str(data_contract.get("source_query_key") or "").strip()
            actual_fields = _value_fields(chart)
            actual_title = str(chart.get("title") or "").strip()
            document = chart.get("chart_document") if isinstance(chart.get("chart_document"), dict) else chart
            actual_kind = str(document.get("chart_kind") or "").strip().lower()
            if not actual_kind:
                chart_spec = chart.get("chart_spec") if isinstance(chart.get("chart_spec"), dict) else {}
                series_kinds = {
                    str(item.get("type") or "").strip().lower()
                    for item in chart_spec.get("series") or []
                    if isinstance(item, dict) and str(item.get("type") or "").strip()
                }
                if len(series_kinds) == 1:
                    actual_kind = next(iter(series_kinds))
            if not actual_kind:
                legacy_kind = str(chart.get("chart_type") or "").strip().lower()
                if legacy_kind != "echarts":
                    actual_kind = legacy_kind
            best_contract_index = -1
            best_score = -1
            for contract_index, contract in enumerate(chart_sources):
                if contract_index in matched_chart_contracts and not contract.get("reusable"):
                    continue
                contract_title = str(contract.get("title") or "").strip()
                contract_kind = str(contract.get("chart_kind") or "").strip().lower()
                contract_fields = set(contract.get("value_fields") or [])
                score = 0
                if contract_title and actual_title:
                    if contract_title != actual_title:
                        continue
                    score += 8
                if contract_kind and actual_kind:
                    if contract_kind != actual_kind:
                        continue
                    score += 3
                if contract_fields and actual_fields:
                    overlap = contract_fields.intersection(actual_fields)
                    if not overlap:
                        continue
                    score += 4
                if score > best_score:
                    best_score = score
                    best_contract_index = contract_index
            if best_contract_index < 0 and len(chart_sources) == len(snapshot.charts):
                best_contract_index = chart_index
            matched_contract = (
                chart_sources[best_contract_index]
                if 0 <= best_contract_index < len(chart_sources)
                else {}
            )
            if best_contract_index >= 0 and not matched_contract.get("reusable"):
                matched_chart_contracts.add(best_contract_index)
            source_frame = str(matched_contract.get("source_frame") or "")
            column_expressions = (
                matched_contract.get("column_expressions")
                if isinstance(matched_contract.get("column_expressions"), dict)
                else {}
            )
            category_field = str(matched_contract.get("category_field") or "").strip()
            for field in actual_fields:
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
                    "_structured_value_expressions": list(
                        column_expressions.get(field) or []
                    ),
                    "_structured_selector_field": category_field,
                    "_structured_selector_expressions": list(
                        column_expressions.get(category_field) or []
                    ),
                    # Kept only while the local compiler resolves a possible
                    # category-scoped calculation boundary. It is removed
                    # before the lineage index is returned.
                    "_structured_rows": rows,
                })

        matched_table_contracts: set[int] = set()
        for table_index, table in enumerate(snapshot.tables):
            rows = table.get("rows") or []
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            actual_title = str(table.get("title") or "").strip()
            contract_index = next(
                (
                    index for index, contract in enumerate(table_sources)
                    if (index not in matched_table_contracts or contract.get("reusable"))
                    and actual_title
                    and str(contract.get("title") or "").strip() == actual_title
                ),
                -1,
            )
            if contract_index < 0:
                reusable_contracts = [
                    index
                    for index, contract in enumerate(table_sources)
                    if contract.get("reusable")
                ]
                if len(reusable_contracts) == 1:
                    contract_index = reusable_contracts[0]
            if contract_index < 0 and len(table_sources) == len(snapshot.tables):
                contract_index = table_index
            matched_contract = (
                table_sources[contract_index]
                if 0 <= contract_index < len(table_sources)
                else {}
            )
            if contract_index >= 0 and not matched_contract.get("reusable"):
                matched_table_contracts.add(contract_index)
            source_frame = str(matched_contract.get("source_frame") or "")
            column_expressions = (
                matched_contract.get("column_expressions")
                if isinstance(matched_contract.get("column_expressions"), dict)
                else {}
            )
            source_query_node_id = self._artifact_datasource_query_node_id(
                snapshot,
                actual_title,
            )
            row_query_node_ids = self._table_row_datasource_query_node_ids(
                snapshot,
                table,
            )
            structured_metric_fields = (
                table_metric_fields[contract_index]
                if 0 <= contract_index < len(table_metric_fields)
                else set()
            )
            for column_index, column in enumerate(table.get("columns") or []):
                key = _table_column_key(column)
                if not key:
                    continue
                label = str(column.get("label") or key) if isinstance(column, dict) else key
                column_values = [row.get(key) for row in rows if key in row]
                sample_values = column_values[:3]
                numeric_display = _structured_numeric_display(column_values)
                if key not in structured_metric_fields and not any(
                    _is_numeric_value(value) or _is_numeric_display_text(str(value).rstrip("%"))
                    or _is_compound_numeric_display_text(value, label)
                    for value in sample_values
                ) and numeric_display is None:
                    continue
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
                    "structured_numeric_display": bool(numeric_display),
                    "display_unit_hint": str(
                        (numeric_display or {}).get("display_unit")
                        or self._explicit_display_unit(label)
                        or ""
                    ),
                    "source_query_node_id": source_query_node_id,
                    "runtime_row_query_node_ids": row_query_node_ids,
                    "source_frame": source_frame,
                    "lineage_fields": [source_field] if source_field else [key],
                    "_structured_value_expressions": list(
                        column_expressions.get(key) or []
                    ),
                    # Kept only while the local compiler resolves a possible
                    # business-group selector for a heterogeneous table column.
                    # It is removed before the lineage index is returned.
                    "_table_rows": rows,
                })

        ast_report_outputs = self._locate_report_outputs(
            snapshot.analysis_report,
            report_expressions,
        )
        reserved_report_spans = {
            (int(output.get("start") or 0), int(output.get("end") or 0))
            for output in ast_report_outputs
            if int(output.get("end") or 0) > int(output.get("start") or 0)
        }
        for output in ast_report_outputs:
            if self._is_known_dimension_output(output, snapshot.selected_datasource_snapshot):
                continue
            output["artifact_id"] = snapshot.report_artifact_id
            outputs.append(output)
        occupied_report_spans = reserved_report_spans.union({
            (int(output.get("start") or 0), int(output.get("end") or 0))
            for output in outputs
            if output.get("artifact_type") == "report"
            and int(output.get("end") or 0) > int(output.get("start") or 0)
        })
        for output in self._locate_report_numeric_candidates(
            snapshot.analysis_report,
            occupied=occupied_report_spans,
        ):
            output["artifact_id"] = snapshot.report_artifact_id
            outputs.append(output)
        return outputs

    @staticmethod
    def _operation_family_signature(operation: dict[str, Any]) -> str:
        source = str(operation.get("source") or "").strip()
        if operation.get("kind") in {"query_projection", "source_field"} and source:
            try:
                expression = parse_one(f"SELECT {source}").expressions[0]
                expression = expression.transform(
                    lambda item: exp.Literal.string("?") if isinstance(item, exp.Literal) else item
                )
                if operation.get("semantic_kind") == "aggregation":
                    expression = expression.transform(
                        lambda item: exp.column("__measure__")
                        if isinstance(item, exp.Column)
                        else item
                    )
                source = expression.sql(pretty=False)
            except Exception:
                source = re.sub(r"(['\"]).*?\1|\b\d+(?:\.\d+)?\b", "?", source)
        elif operation.get("kind") == "calculation" and source:
            try:
                parsed = ast.parse(source)
                statement = parsed.body[0]
                expression = getattr(statement, "value", statement)

                class _ComparisonValueNormalizer(ast.NodeTransformer):
                    def visit_Compare(self, node: ast.Compare) -> ast.AST:  # noqa: N802
                        node = self.generic_visit(node)
                        node.comparators = [
                            ast.Constant(value="?")
                            if isinstance(item, ast.Constant)
                            else item
                            for item in node.comparators
                        ]
                        return node

                normalized_expression = _ComparisonValueNormalizer().visit(expression)
                source = ast.dump(normalized_expression, include_attributes=False)
            except (SyntaxError, IndexError, AttributeError):
                pass
        return _stable_json({
            "kind": operation.get("semantic_kind") or "",
            "source": source,
        })

    @staticmethod
    def _operation_write_signature(operation: dict[str, Any]) -> str:
        """Return the structural assignment target for a calculation operation."""
        source = str(operation.get("source") or "").strip()
        if operation.get("kind") != "calculation" or not source:
            return ""
        try:
            statement = ast.parse(source).body[0]
        except (SyntaxError, IndexError):
            return ""
        targets: list[ast.AST] = []
        if isinstance(statement, ast.Assign):
            targets.extend(statement.targets)
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            targets.append(statement.target)
        if not targets:
            return ""
        return _stable_json([
            ast.dump(target, include_attributes=False)
            for target in targets
        ])

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

        assignment_values: dict[str, ast.AST] = {}
        for assignment in sorted(
            (
                item for item in ast.walk(tree)
                if isinstance(item, (ast.Assign, ast.AnnAssign))
            ),
            key=lambda item: int(getattr(item, "lineno", 0) or 0),
        ):
            value = getattr(assignment, "value", None)
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    assignment_values[target.id] = value

        operation_by_id = {
            str(item.get("operation_node_id")): item
            for item in operations
            if str(item.get("operation_node_id") or "")
        }
        query_row_count_operations = {
            str(item.get("query_node_id") or ""): item
            for item in operations
            if item.get("kind") == "query_result_row_count"
            and str(item.get("query_node_id") or "")
        }
        operation_producers: dict[str, set[str]] = {}
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                target_name = str(target or "").strip()
                if target_name:
                    operation_producers.setdefault(target_name, set()).add(operation_id)
        operation_producer_lists = {
            name: list(producer_ids)
            for name, producer_ids in operation_producers.items()
        }
        operations_by_line: dict[int, list[dict[str, Any]]] = {}
        for operation in operation_by_id.values():
            operations_by_line.setdefault(int(operation.get("line") or 0), []).append(operation)

        def empty_origin() -> dict[str, set[str]]:
            return {
                "queries": set(),
                "boundaries": set(),
                "operations": set(),
                "dimension_boundaries": set(),
                "fields": set(),
            }

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
                    "fields": {str(field)},
                }

        query_bindings = cls._query_expression_bindings(tree, query_snapshots)
        frame_columns: dict[str, dict[str, dict[str, set[str]]]] = {}
        frame_positions: dict[str, list[dict[str, set[str]]]] = {}
        scalar_origins: dict[str, dict[str, set[str]]] = {}
        mapping_value_origins: dict[str, dict[str, set[str]]] = {}
        mapping_field_origins: dict[str, dict[str, dict[str, set[str]]]] = {}
        mapping_alias_sources: dict[str, str] = {}
        function_frame_summaries: dict[str, dict[str, Any]] = {}

        def function_default_strings(function: ast.FunctionDef) -> dict[str, str]:
            positional = [item.arg for item in function.args.args]
            defaults = list(function.args.defaults)
            offset = len(positional) - len(defaults)
            result: dict[str, str] = {}
            for index, default in enumerate(defaults, start=offset):
                if (
                    0 <= index < len(positional)
                    and isinstance(default, ast.Constant)
                    and isinstance(default.value, str)
                ):
                    result[positional[index]] = str(default.value)
            return result

        def frame_transform_summary(
            function: ast.FunctionDef,
        ) -> dict[str, Any] | None:
            parameter_names = [item.arg for item in function.args.args]
            if not parameter_names:
                return None
            defaults = function_default_strings(function)
            alias_sources = {name: name for name in parameter_names}
            scalar_dependencies: dict[str, dict[str, Any]] = {}
            derived_fields: dict[str, dict[str, Any]] = {}

            def static_field(node: ast.Subscript) -> str:
                if isinstance(node.slice, ast.Constant) and isinstance(
                    node.slice.value, str
                ):
                    return str(node.slice.value)
                if isinstance(node.slice, ast.Name):
                    if node.slice.id in parameter_names:
                        return f"@parameter:{node.slice.id}"
                    return ""
                return ""

            def expression_dependencies(
                node: ast.AST | None,
                source_parameter: str,
            ) -> dict[str, Any]:
                source_fields: list[str] = []
                operation_ids: list[str] = []
                if node is None:
                    return {"source_fields": [], "operation_ids": []}
                for item in ast.walk(node):
                    if (
                        isinstance(item, ast.Subscript)
                        and isinstance(item.value, ast.Name)
                        and alias_sources.get(item.value.id) == source_parameter
                    ):
                        field = static_field(item)
                        if field:
                            source_fields.append(field)
                    elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
                        dependency = scalar_dependencies.get(item.id)
                        if dependency and dependency.get(
                            "source_parameter"
                        ) == source_parameter:
                            source_fields.extend(dependency["source_fields"])
                            operation_ids.extend(dependency["operation_ids"])
                return {
                    "source_fields": list(dict.fromkeys(source_fields)),
                    "operation_ids": list(dict.fromkeys(operation_ids)),
                }

            statements = sorted(
                (
                    item
                    for item in ast.walk(function)
                    if isinstance(item, (ast.Assign, ast.AnnAssign))
                ),
                key=lambda item: int(getattr(item, "lineno", 0) or 0),
            )
            for statement in statements:
                value = getattr(statement, "value", None)
                targets = (
                    list(statement.targets)
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                simple_targets = [
                    target.id for target in targets if isinstance(target, ast.Name)
                ]
                if len(simple_targets) == 1:
                    target_name = simple_targets[0]
                    source_alias = cls._expression_base_frame(value)
                    if source_alias in alias_sources:
                        alias_sources[target_name] = alias_sources[source_alias]
                    dependency_sources = {
                        alias_sources[item.value.id]
                        for item in ast.walk(value) if value is not None
                        if isinstance(item, ast.Subscript)
                        and isinstance(item.value, ast.Name)
                        and item.value.id in alias_sources
                    }
                    dependency_source = (
                        next(iter(dependency_sources))
                        if len(dependency_sources) == 1
                        else ""
                    )
                    dependencies = expression_dependencies(
                        value,
                        dependency_source,
                    )
                    operation = operation_for(statement)
                    operation_id = str(
                        (operation or {}).get("operation_node_id") or ""
                    )
                    if (
                        operation_id
                        and (operation or {}).get("semantic_kind")
                        in {"aggregation", "formula", "ranking", "projection"}
                    ):
                        dependencies["operation_ids"].append(operation_id)
                    if dependencies["source_fields"]:
                        scalar_dependencies[target_name] = {
                            "source_parameter": dependency_source,
                            "source_fields": list(dict.fromkeys(
                                dependencies["source_fields"]
                            )),
                            "operation_ids": list(dict.fromkeys(
                                dependencies["operation_ids"]
                            )),
                        }

                for target in targets:
                    if not (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in alias_sources
                    ):
                        continue
                    source_parameter = alias_sources[target.value.id]
                    field = static_field(target)
                    if not field:
                        continue
                    dependencies = expression_dependencies(value, source_parameter)
                    operation = operation_for(statement)
                    operation_id = str(
                        (operation or {}).get("operation_node_id") or ""
                    )
                    if (
                        operation_id
                        and (operation or {}).get("semantic_kind")
                        in {"aggregation", "formula", "ranking", "projection"}
                    ):
                        dependencies["operation_ids"].append(operation_id)
                    if dependencies["source_fields"]:
                        derived_fields[field] = {
                            "source_parameter": source_parameter,
                            "source_fields": list(dict.fromkeys(
                                dependencies["source_fields"]
                            )),
                            "operation_ids": list(dict.fromkeys(
                                dependencies["operation_ids"]
                            )),
                        }

            returned_source_parameters = {
                alias_sources.get(cls._expression_base_frame(item.value), "")
                for item in ast.walk(function)
                if isinstance(item, ast.Return)
            }
            returned_source_parameters.discard("")
            eligible_parameters = {
                str(item.get("source_parameter") or "")
                for item in derived_fields.values()
            }.intersection(returned_source_parameters)
            if not derived_fields or len(eligible_parameters) != 1:
                return None
            source_parameter = next(iter(eligible_parameters))
            return {
                "kind": "frame_transform",
                "parameter_index": parameter_names.index(source_parameter),
                "parameter_name": source_parameter,
                "parameter_names": parameter_names,
                "parameter_defaults": defaults,
                "derived_fields": {
                    field: item
                    for field, item in derived_fields.items()
                    if item.get("source_parameter") == source_parameter
                },
            }

        for function in (
            item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef)
        ):
            transform_summary = frame_transform_summary(function)
            if transform_summary is not None:
                function_frame_summaries[function.name] = transform_summary
                continue
            parameter_names = [item.arg for item in function.args.args]
            if not parameter_names:
                continue
            local_assignments = {
                target.id: statement
                for statement in ast.walk(function)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                for target in (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            local_frame_sources = {name: name for name in parameter_names}
            for statement in sorted(
                local_assignments.values(),
                key=lambda item: int(getattr(item, "lineno", 0) or 0),
            ):
                value = getattr(statement, "value", None)
                source_name = cls._expression_base_frame(value)
                source_parameter = local_frame_sources.get(source_name, "")
                if not source_parameter:
                    continue
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        local_frame_sources[target.id] = source_parameter
            for return_node in (
                item for item in ast.walk(function) if isinstance(item, ast.Return)
            ):
                return_value = return_node.value
                returned_name = cls._expression_base_frame(return_value)
                assignment = local_assignments.get(returned_name)
                assigned_value = getattr(assignment, "value", None)
                if assignment is None or assigned_value is None:
                    continue
                groupby_call = next((
                    item for item in ast.walk(assigned_value)
                    if isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and item.func.attr == "groupby"
                ), None)
                aggregate_call = next((
                    item for item in ast.walk(assigned_value)
                    if isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and item.func.attr in {"agg", "aggregate"}
                ), None)
                if groupby_call is None or aggregate_call is None:
                    continue
                source_frame = cls._expression_base_frame(groupby_call.func.value)
                source_parameter = local_frame_sources.get(source_frame, source_frame)
                if source_parameter not in parameter_names:
                    continue
                output_fields: dict[str, dict[str, Any]] = {}
                for argument in groupby_call.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        output_fields[str(argument.value)] = {
                            "source_field": str(argument.value),
                            "dimension": True,
                        }
                    elif isinstance(argument, (ast.List, ast.Tuple)):
                        for item in argument.elts:
                            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                output_fields[str(item.value)] = {
                                    "source_field": str(item.value),
                                    "dimension": True,
                                }
                for keyword in aggregate_call.keywords:
                    if not keyword.arg or not isinstance(keyword.value, (ast.Tuple, ast.List)):
                        continue
                    elements = list(keyword.value.elts)
                    if not elements:
                        continue
                    source_field = (
                        str(elements[0].value)
                        if isinstance(elements[0], ast.Constant)
                        and isinstance(elements[0].value, str)
                        else ""
                    )
                    if source_field:
                        output_fields[str(keyword.arg)] = {
                            "source_field": source_field,
                            "dimension": False,
                        }
                if not output_fields:
                    continue
                operation = operation_for(assignment)
                function_frame_summaries[function.name] = {
                    "parameter_index": parameter_names.index(source_parameter),
                    "output_fields": output_fields,
                    "operation_id": str(
                        (operation or {}).get("operation_node_id") or ""
                    ),
                }
                break
        for loop in (item for item in ast.walk(tree) if isinstance(item, ast.For)):
            if not (
                isinstance(loop.target, (ast.Tuple, ast.List))
                and len(loop.target.elts) >= 2
                and isinstance(loop.target.elts[1], ast.Name)
                and isinstance(loop.iter, ast.Call)
                and isinstance(loop.iter.func, ast.Attribute)
                and loop.iter.func.attr == "items"
                and isinstance(loop.iter.func.value, ast.Name)
            ):
                continue
            mapping_alias_sources[loop.target.elts[1].id] = loop.iter.func.value.id
        string_sequences: dict[str, list[str]] = {}
        dynamic_subscript_fields: dict[str, list[str]] = {}
        appended_record_dicts: dict[str, list[ast.Dict]] = {}
        record_table_frames: dict[str, str] = {}
        literal_dataframe_record_rows: dict[str, list[dict[str, Any]]] = {}
        appended_frame_sources: dict[str, list[str]] = {}
        for assignment in (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.Assign, ast.AnnAssign))
        ):
            value = getattr(assignment, "value", None)
            targets = getattr(assignment, "targets", None) or [getattr(assignment, "target", None)]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if (
                len(names) == 1
                and isinstance(value, (ast.List, ast.Tuple))
                and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in value.elts
                )
            ):
                string_sequences[names[0]] = [str(item.value) for item in value.elts]
            if (
                len(names) == 1
                and isinstance(value, (ast.List, ast.Tuple))
                and all(isinstance(item, ast.Dict) for item in value.elts)
            ):
                # A named records list is semantically identical to a sequence
                # built with ``rows.append({...})``. Keep every row expression
                # so formatted values do not collapse into one column-wide
                # calculation boundary.
                appended_record_dicts[names[0]] = list(value.elts)
            if (
                len(names) == 1
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "DataFrame"
                and value.args
                and isinstance(value.args[0], ast.Name)
            ):
                record_table_frames[names[0]] = value.args[0].id
        for loop in (item for item in ast.walk(tree) if isinstance(item, ast.For)):
            if not isinstance(loop.target, (ast.Tuple, ast.List)):
                continue
            if not (
                isinstance(loop.iter, ast.Call)
                and isinstance(loop.iter.func, ast.Name)
                and loop.iter.func.id == "zip"
            ):
                continue
            target_names = [
                item.id if isinstance(item, ast.Name) else ""
                for item in loop.target.elts
            ]
            for index, iterator in enumerate(loop.iter.args):
                if index >= len(target_names) or not target_names[index]:
                    continue
                if isinstance(iterator, ast.Name) and iterator.id in string_sequences:
                    dynamic_subscript_fields[target_names[index]] = list(
                        string_sequences[iterator.id]
                    )
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            if not (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "append"
                and isinstance(call.func.value, ast.Name)
                and len(call.args) == 1
            ):
                continue
            if isinstance(call.args[0], ast.Dict):
                appended_record_dicts.setdefault(call.func.value.id, []).append(call.args[0])
            elif isinstance(call.args[0], ast.Name):
                appended_frame_sources.setdefault(call.func.value.id, []).append(
                    call.args[0].id
                )
        row_frames: list[tuple[str, str, int, int]] = []
        group_frame_sources: dict[str, str] = {}
        enumeration_index_scopes: list[tuple[str, int, int]] = []
        iteration_alias_scopes: list[
            tuple[str, int, int, dict[str, set[str]]]
        ] = []
        comprehension_owners = {
            id(generator): expression
            for expression in ast.walk(tree)
            if isinstance(expression, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
            for generator in expression.generators
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.comprehension)):
                continue
            scope_node = comprehension_owners.get(id(node), node)
            if (
                isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Name)
                and node.iter.func.id == "enumerate"
                and isinstance(node.target, (ast.Tuple, ast.List))
                and node.target.elts
                and isinstance(node.target.elts[0], ast.Name)
            ):
                enumeration_index_scopes.append((
                    node.target.elts[0].id,
                    int(getattr(scope_node, "lineno", 0) or 0),
                    int(getattr(scope_node, "end_lineno", getattr(scope_node, "lineno", 0)) or 0),
                ))
            source_frame = cls._record_iterator_source_frame(node.iter)
            if not source_frame:
                continue
            if (
                isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Attribute)
                and node.iter.func.attr == "groupby"
                and isinstance(node.target, (ast.Tuple, ast.List))
                and len(node.target.elts) >= 2
                and isinstance(node.target.elts[1], ast.Name)
            ):
                group_frame_sources[node.target.elts[1].id] = source_frame
            targets = []
            if isinstance(node.target, ast.Name):
                targets = [node.target.id]
            elif isinstance(node.target, (ast.Tuple, ast.List)):
                targets = [
                    item.id
                    for item in ast.walk(node.target)
                    if isinstance(item, ast.Name)
                ]
            start = int(getattr(scope_node, "lineno", 0) or 0)
            end = int(getattr(scope_node, "end_lineno", start) or start)
            for target in targets:
                if target != "_":
                    row_frames.append((target, source_frame, start, end))
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            if not (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "apply"
                and call.args
                and isinstance(call.args[0], ast.Lambda)
            ):
                continue
            source_frame = cls._expression_base_frame(call.func.value)
            if not source_frame:
                continue
            lambda_node = call.args[0]
            start = int(getattr(lambda_node, "lineno", getattr(call, "lineno", 0)) or 0)
            end = int(getattr(lambda_node, "end_lineno", getattr(call, "end_lineno", start)) or start)
            for argument in lambda_node.args.args:
                row_frames.append((argument.arg, source_frame, start, end))

        def row_source(name: str, line: int) -> str:
            matches = [
                (end - start, frame)
                for alias, frame, start, end in row_frames
                if alias == name and start <= line <= end
            ]
            return min(matches, default=(0, ""))[1]

        def is_presentation_ordinal(names: set[str], line: int) -> bool:
            return bool(names) and all(
                any(alias == name and start <= line <= end for alias, start, end in enumeration_index_scopes)
                for name in names
            )

        def iteration_alias_origin(
            name: str,
            line: int,
        ) -> dict[str, set[str]] | None:
            matches = [
                (end - start, origin)
                for alias, start, end, origin in iteration_alias_scopes
                if alias == name and start <= line <= end
            ]
            return min(matches, default=(0, None))[1]

        def subscript_key(node: ast.Subscript) -> str:
            value = node.slice
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return str(value.value)
            return ""

        def static_string(node: ast.AST | None) -> str:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return str(node.value)
            if isinstance(node, ast.JoinedStr) and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.values
            ):
                return "".join(str(item.value) for item in node.values)
            return ""

        def expression_origin(node: ast.AST | None, line: int) -> dict[str, set[str]]:
            if node is None:
                return empty_origin()
            origins: list[dict[str, set[str]]] = []
            column_access_frame_names = {
                item.value.id
                for item in ast.walk(node)
                if isinstance(item, ast.Subscript)
                and isinstance(item.value, ast.Name)
            }
            for item in ast.walk(node):
                if (
                    isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Subscript)
                    and isinstance(item.value.value, ast.Attribute)
                    and item.value.value.attr in {"loc", "iloc", "iat", "at"}
                ):
                    # Pandas scalar projections are commonly written as
                    # ``frame.iloc[0]["metric"]`` (and equivalently through
                    # ``loc``/``iat``/``at``).  The outer field subscription
                    # still belongs to the original frame even though its
                    # immediate receiver is another subscription rather than
                    # an ``ast.Name``.  Resolve the base frame before the
                    # name-only mapping branch below so the selected query
                    # column remains connected to downstream formulas and
                    # structured artifact cells.
                    field = subscript_key(item)
                    frame_name = cls._expression_base_frame(item.value.value.value)
                    if field and frame_name in frame_columns:
                        origin = (frame_columns.get(frame_name) or {}).get(field)
                        if origin:
                            origins.append(origin)
                if (
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and item.func.attr == "get"
                    and isinstance(item.func.value, ast.Name)
                ):
                    owner_name = item.func.value.id
                    field = static_string(item.args[0]) if item.args else ""
                    source_frame = row_source(
                        owner_name,
                        int(getattr(item, "lineno", line) or line),
                    )
                    origin = (
                        (frame_columns.get(source_frame) or {}).get(field)
                        if source_frame and field
                        else mapping_value_origins.get(owner_name)
                    )
                    if origin:
                        origins.append(origin)
                if (
                    isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Attribute)
                    and item.value.attr in {"loc", "iloc"}
                ):
                    frame_name = cls._expression_base_frame(item.value.value)
                    if not frame_name:
                        continue
                    slice_items = (
                        list(item.slice.elts)
                        if isinstance(item.slice, (ast.Tuple, ast.List))
                        else [item.slice]
                    )
                    for slice_item in slice_items:
                        if not (
                            isinstance(slice_item, ast.Constant)
                            and isinstance(slice_item.value, str)
                        ):
                            continue
                        origin = (frame_columns.get(frame_name) or {}).get(
                            str(slice_item.value)
                        )
                        if origin:
                            origins.append(origin)
                if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name):
                    frame_name = row_source(
                        item.value.id,
                        int(getattr(item, "lineno", line) or line),
                    )
                    if frame_name:
                        origin = (frame_columns.get(frame_name) or {}).get(item.attr)
                        if origin:
                            origins.append(origin)
                if isinstance(item, ast.Subscript) and isinstance(item.value, ast.Name):
                    field = subscript_key(item)
                    frame_name = item.value.id
                    mapping_source = mapping_alias_sources.get(frame_name, frame_name)
                    if field:
                        mapping_origin = (
                            mapping_field_origins.get(mapping_source) or {}
                        ).get(field)
                        if mapping_origin:
                            origins.append(mapping_origin)
                            continue
                    frame_name = (
                        row_source(frame_name, line)
                        or row_source(
                            frame_name,
                            int(getattr(item, "lineno", line) or line),
                        )
                        or frame_name
                    )
                    if field:
                        origin = (frame_columns.get(frame_name) or {}).get(field)
                        if origin:
                            origins.append(origin)
                            continue
                        # A mapping selected through ``mapping.get(key)`` is a
                        # scalar record, not a DataFrame.  Its member access
                        # retains the record's upstream query even when the
                        # dynamic outer key prevents a field-specific mapping.
                        # Keep that proven origin available for the semantic
                        # resolver instead of dropping the whole query path.
                        scalar_origin = scalar_origins.get(mapping_source)
                        if scalar_origin:
                            origins.append(scalar_origin)
                        continue
                    if isinstance(item.slice, (ast.List, ast.Tuple)):
                        for field_node in item.slice.elts:
                            if not (
                                isinstance(field_node, ast.Constant)
                                and isinstance(field_node.value, str)
                            ):
                                continue
                            origin = (frame_columns.get(frame_name) or {}).get(
                                str(field_node.value)
                            )
                            if origin:
                                origins.append(origin)
                        continue
                    if isinstance(item.slice, ast.Name):
                        for candidate_field in dynamic_subscript_fields.get(item.slice.id, []):
                            origin = (frame_columns.get(frame_name) or {}).get(candidate_field)
                            if origin:
                                origins.append(origin)
                elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
                    if item.id in column_access_frame_names:
                        continue
                    origin = (
                        iteration_alias_origin(item.id, line)
                        or scalar_origins.get(item.id)
                    )
                    if origin:
                        origins.append(origin)
            return merge_origins(*origins)

        def frame_name_from_expression(node: ast.AST | None) -> str:
            if isinstance(node, ast.Name):
                candidate = group_frame_sources.get(node.id, node.id)
                return candidate if candidate in frame_columns else ""
            if isinstance(node, ast.IfExp):
                branch_names = {
                    name
                    for name in (
                        frame_name_from_expression(node.body),
                        frame_name_from_expression(node.orelse),
                    )
                    if name
                }
                return next(iter(branch_names)) if len(branch_names) == 1 else ""
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

            # Keep positional lineage aligned with the common pandas pattern
            # ``frame.columns = [...]``.  The aggregation was already traced on
            # the preceding assignment; this statement only gives its output
            # columns their business-facing names.
            renamed_columns_target = next(
                (
                    target.value.id
                    for target in raw_targets
                    if isinstance(target, ast.Attribute)
                    and target.attr == "columns"
                    and isinstance(target.value, ast.Name)
                ),
                "",
            )
            if (
                renamed_columns_target
                and isinstance(value, (ast.List, ast.Tuple))
                and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in value.elts
                )
            ):
                positions = frame_positions.get(renamed_columns_target) or []
                names = [str(item.value) for item in value.elts]
                if len(positions) == len(names):
                    frame_columns[renamed_columns_target] = {
                        name: merge_origins(positions[index])
                        for index, name in enumerate(names)
                    }
                continue

            simple_targets = [target.id for target in raw_targets if isinstance(target, ast.Name)]

            if len(simple_targets) == 1:
                target = simple_targets[0]
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in function_frame_summaries
                ):
                    summary = function_frame_summaries[value.func.id]
                    parameter_index = int(summary.get("parameter_index") or 0)
                    parameter_name = str(summary.get("parameter_name") or "")
                    source_expression = next(
                        (
                            keyword.value
                            for keyword in value.keywords
                            if keyword.arg == parameter_name
                        ),
                        value.args[parameter_index]
                        if parameter_index < len(value.args)
                        else None,
                    )
                    source_frame = frame_name_from_expression(source_expression)
                    if source_frame:
                        if summary.get("kind") == "frame_transform":
                            copy_frame(target, source_frame)
                            parameter_names = list(
                                summary.get("parameter_names") or []
                            )
                            parameter_bindings = dict(
                                summary.get("parameter_defaults") or {}
                            )
                            for index, argument in enumerate(value.args):
                                if (
                                    index < len(parameter_names)
                                    and isinstance(argument, ast.Constant)
                                    and isinstance(argument.value, str)
                                ):
                                    parameter_bindings[parameter_names[index]] = str(
                                        argument.value
                                    )
                            for keyword in value.keywords:
                                if (
                                    keyword.arg
                                    and isinstance(keyword.value, ast.Constant)
                                    and isinstance(keyword.value.value, str)
                                ):
                                    parameter_bindings[keyword.arg] = str(
                                        keyword.value.value
                                    )
                            for output_field, field_summary in (
                                summary.get("derived_fields") or {}
                            ).items():
                                source_fields = [
                                    parameter_bindings.get(
                                        field.removeprefix("@parameter:"),
                                        "",
                                    )
                                    if field.startswith("@parameter:")
                                    else field
                                    for field in (
                                        (field_summary or {}).get("source_fields") or []
                                    )
                                ]
                                source_fields = [
                                    field for field in source_fields if field
                                ]
                                origin = merge_origins(*(
                                    (frame_columns.get(target) or {}).get(field)
                                    for field in source_fields
                                ))
                                if not origin.get("queries"):
                                    continue
                                operation_ids = {
                                    str(item)
                                    for item in (
                                        (field_summary or {}).get("operation_ids") or []
                                    )
                                    if str(item) in operation_by_id
                                }
                                origin["operations"].update(operation_ids)
                                if operation_ids:
                                    origin["boundaries"] = operation_ids
                                frame_columns.setdefault(target, {})[
                                    str(output_field)
                                ] = origin
                            frame_positions[target] = [
                                frame_columns[target][field]
                                for field in frame_columns[target]
                            ]
                            scalar_origins[target] = merge_origins(
                                *frame_columns[target].values()
                            )
                            continue
                        output_columns: dict[str, dict[str, set[str]]] = {}
                        operation_id = str(summary.get("operation_id") or "")
                        for output_field, field_summary in (
                            summary.get("output_fields") or {}
                        ).items():
                            source_field = str(
                                (field_summary or {}).get("source_field") or ""
                            )
                            origin = merge_origins(
                                (frame_columns.get(source_frame) or {}).get(source_field)
                            )
                            if not origin.get("queries"):
                                continue
                            if operation_id:
                                origin["operations"].add(operation_id)
                                if not bool((field_summary or {}).get("dimension")):
                                    origin["boundaries"] = {operation_id}
                            output_columns[str(output_field)] = origin
                        if output_columns:
                            frame_columns[target] = output_columns
                            frame_positions[target] = [
                                output_columns[field] for field in output_columns
                            ]
                            scalar_origins[target] = merge_origins(
                                *output_columns.values()
                            )
                            continue
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "get"
                    and isinstance(value.func.value, ast.Name)
                ):
                    # ``record = records.get(dynamic_key, default)`` preserves
                    # the field-level lineage accumulated on ``records``.  The
                    # selected outer key chooses a record; it does not merge
                    # the independent metric boundaries of that record's
                    # members.
                    mapping_alias_sources[target] = value.func.value.id
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
                            {
                                "queries": {query_id},
                                "boundaries": set(),
                                "operations": set(),
                                "fields": {column},
                            },
                        )
                        for column in columns
                    }
                    frame_positions[target] = [frame_columns[target][column] for column in columns]
                    continue

                dataframe_value_call = next(
                    (
                        item for item in ast.walk(value)
                        if isinstance(item, ast.Call)
                        and (
                            (isinstance(item.func, ast.Name) and item.func.id == "DataFrame")
                            or (isinstance(item.func, ast.Attribute) and item.func.attr == "DataFrame")
                        )
                        and item.args
                        and (
                            isinstance(item.args[0], ast.Dict)
                            or (
                                isinstance(item.args[0], (ast.List, ast.Tuple))
                                and item.args[0].elts
                                and all(isinstance(record, ast.Dict) for record in item.args[0].elts)
                            )
                            or (
                                isinstance(item.args[0], ast.Name)
                                and bool(appended_record_dicts.get(item.args[0].id))
                            )
                        )
                    ),
                    None,
                ) if value is not None else None
                if (
                    dataframe_value_call is not None
                ):
                    dataframe_argument = dataframe_value_call.args[0]
                    mapped: dict[str, dict[str, set[str]]] = {}
                    records = (
                        [dataframe_argument]
                        if isinstance(dataframe_argument, ast.Dict)
                        else list(appended_record_dicts.get(dataframe_argument.id) or [])
                        if isinstance(dataframe_argument, ast.Name)
                        else list(dataframe_argument.elts)
                    )
                    record_container = (
                        dataframe_argument.id
                        if isinstance(dataframe_argument, ast.Name)
                        else ""
                    )
                    literal_rows: list[dict[str, Any]] = []
                    if isinstance(dataframe_argument, ast.Dict):
                        fields = [static_string(key) for key in dataframe_argument.keys]
                        sequences = [
                            list(item.elts) if isinstance(item, (ast.List, ast.Tuple)) else []
                            for item in dataframe_argument.values
                        ]
                        row_count = min((len(items) for items in sequences), default=0)
                        if (
                            row_count >= 2
                            and all(fields)
                            and all(len(items) == row_count for items in sequences)
                        ):
                            for row_index in range(row_count):
                                row_values: dict[str, Any] = {}
                                row_origins: dict[str, dict[str, set[str]]] = {}
                                for field, items in zip(fields, sequences):
                                    item = items[row_index]
                                    if isinstance(item, ast.Constant) and isinstance(
                                        item.value, (str, int, float, bool)
                                    ):
                                        row_values[field] = item.value
                                    row_origins[field] = expression_origin(
                                        item,
                                        int(getattr(item, "lineno", line) or line),
                                    )
                                literal_rows.append({
                                    "values": row_values,
                                    "origins": row_origins,
                                })
                    elif isinstance(dataframe_argument, (ast.List, ast.Tuple)):
                        for record in dataframe_argument.elts:
                            if not isinstance(record, ast.Dict):
                                continue
                            row_values: dict[str, Any] = {}
                            row_origins: dict[str, dict[str, set[str]]] = {}
                            for key_node, value_node in zip(record.keys, record.values):
                                field = static_string(key_node)
                                if not field:
                                    continue
                                if isinstance(value_node, ast.Constant) and isinstance(
                                    value_node.value, (str, int, float, bool)
                                ):
                                    row_values[field] = value_node.value
                                row_origins[field] = expression_origin(
                                    value_node,
                                    int(getattr(value_node, "lineno", line) or line),
                                )
                            if row_values and row_origins:
                                literal_rows.append({
                                    "values": row_values,
                                    "origins": row_origins,
                                })
                    if len(literal_rows) >= 2:
                        literal_dataframe_record_rows[target] = literal_rows
                    max_columns = max((len(record.keys) for record in records), default=0)
                    positions: list[dict[str, set[str]]] = []
                    for column_index in range(max_columns):
                        column_origins: list[dict[str, set[str]]] = []
                        for record in records:
                            if column_index >= len(record.values):
                                continue
                            value_node = record.values[column_index]
                            field_origin = expression_origin(
                                value_node,
                                int(getattr(value_node, "lineno", line) or line),
                            )
                            key_node = (
                                record.keys[column_index]
                                if column_index < len(record.keys)
                                else None
                            )
                            field = static_string(key_node)
                            if record_container and field and field_origin.get("queries"):
                                write_target = f"{record_container}[{field!r}]"
                                value_line = int(getattr(value_node, "lineno", line) or line)
                                write_operation = next(
                                    (
                                        operation
                                        for operation in operations_by_line.get(value_line, [])
                                        if write_target in (operation.get("targets") or [])
                                        and operation.get("semantic_kind")
                                        in {"aggregation", "formula", "ranking"}
                                    ),
                                    None,
                                )
                                if write_operation is not None:
                                    operation_id = str(
                                        write_operation.get("operation_node_id") or ""
                                    )
                                    if operation_id:
                                        field_origin["operations"].add(operation_id)
                                        field_origin["boundaries"] = {operation_id}
                            column_origins.append(field_origin)
                        origin = merge_origins(*column_origins)
                        positions.append(origin)
                        first_key = next(
                            (
                                record.keys[column_index]
                                for record in records
                                if column_index < len(record.keys)
                            ),
                            None,
                        )
                        field = static_string(first_key)
                        if field:
                            mapped[field] = origin
                    frame_columns[target] = mapped
                    frame_positions[target] = positions
                    continue

                # Preserve per-column provenance when rows selected from a
                # query-backed frame are accumulated in a list and rebuilt as
                # a DataFrame. This is a common shape for "best row per group"
                # calculations and is semantically equivalent to a projection
                # of the appended row frames.
                appended_frame_dataframe_call = next(
                    (
                        item for item in ast.walk(value)
                        if isinstance(item, ast.Call)
                        and (
                            (isinstance(item.func, ast.Name) and item.func.id == "DataFrame")
                            or (isinstance(item.func, ast.Attribute) and item.func.attr == "DataFrame")
                        )
                        and item.args
                        and isinstance(item.args[0], ast.Name)
                        and item.args[0].id in appended_frame_sources
                    ),
                    None,
                ) if value is not None else None
                if appended_frame_dataframe_call is not None:
                    container_name = appended_frame_dataframe_call.args[0].id
                    source_frames = list(dict.fromkeys(
                        appended_frame_sources.get(container_name) or []
                    ))
                    mapped: dict[str, dict[str, set[str]]] = {}
                    for source in source_frames:
                        for field, origin in (frame_columns.get(source) or {}).items():
                            mapped[field] = merge_origins(mapped.get(field), origin)
                    if mapped:
                        frame_columns[target] = mapped
                        frame_positions[target] = [
                            merge_origins(origin) for origin in mapped.values()
                        ]
                        continue

                concat_call = next(
                    (
                        item for item in ast.walk(value)
                        if isinstance(item, ast.Call)
                        and (
                            (isinstance(item.func, ast.Name) and item.func.id == "concat")
                            or (
                                isinstance(item.func, ast.Attribute)
                                and item.func.attr == "concat"
                            )
                        )
                        and item.args
                    ),
                    None,
                ) if value is not None else None
                if concat_call is not None:
                    concat_input = concat_call.args[0]
                    source_frames: list[str] = []
                    if isinstance(concat_input, ast.Name):
                        source_frames.extend(
                            appended_frame_sources.get(concat_input.id) or []
                        )
                    elif isinstance(concat_input, (ast.List, ast.Tuple)):
                        source_frames.extend(
                            item.id
                            for item in concat_input.elts
                            if isinstance(item, ast.Name)
                        )
                    source_frames = list(dict.fromkeys(source_frames))
                    concatenated_columns: dict[str, dict[str, set[str]]] = {}
                    column_order: list[str] = []
                    for source in source_frames:
                        for field, origin in (frame_columns.get(source) or {}).items():
                            if field not in concatenated_columns:
                                column_order.append(field)
                            concatenated_columns[field] = merge_origins(
                                concatenated_columns.get(field),
                                origin,
                            )
                    if concatenated_columns:
                        frame_columns[target] = concatenated_columns
                        frame_positions[target] = [
                            merge_origins(concatenated_columns[field])
                            for field in column_order
                        ]
                        continue

                # Preserve lineage for ``DataFrame(named_sequence,
                # columns=[...])``. Ranked ``dict.items()`` results commonly
                # use this shape before becoming a chart data frame. The named
                # sequence already owns the calculation/query origin; the
                # explicit columns only assign positional labels.
                sequence_dataframe_call = next(
                    (
                        item for item in ast.walk(value)
                        if isinstance(item, ast.Call)
                        and (
                            (isinstance(item.func, ast.Name) and item.func.id == "DataFrame")
                            or (isinstance(item.func, ast.Attribute) and item.func.attr == "DataFrame")
                        )
                        and item.args
                        and isinstance(item.args[0], ast.Name)
                        and item.args[0].id in scalar_origins
                    ),
                    None,
                ) if value is not None else None
                if sequence_dataframe_call is not None:
                    columns_node = next(
                        (
                            keyword.value
                            for keyword in sequence_dataframe_call.keywords
                            if keyword.arg == "columns"
                        ),
                        None,
                    )
                    if (
                        isinstance(columns_node, (ast.List, ast.Tuple))
                        and columns_node.elts
                        and all(
                            isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                            for item in columns_node.elts
                        )
                    ):
                        sequence_origin = merge_origins(
                            scalar_origins.get(sequence_dataframe_call.args[0].id)
                        )
                        column_names = [str(item.value) for item in columns_node.elts]
                        frame_columns[target] = {
                            column_name: merge_origins(sequence_origin)
                            for column_name in column_names
                        }
                        frame_positions[target] = [
                            frame_columns[target][column_name]
                            for column_name in column_names
                        ]
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

                groupby_multi_agg_call = next(
                    (
                        item for item in ast.walk(value)
                        if isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr in {"agg", "aggregate"}
                        and item.args
                        and isinstance(item.args[0], (ast.List, ast.Tuple))
                    ),
                    None,
                ) if value is not None else None
                groupby_call_for_multi_agg = next(
                    (
                        item for item in ast.walk(value)
                        if isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr == "groupby"
                    ),
                    None,
                ) if groupby_multi_agg_call is not None else None
                if groupby_multi_agg_call is not None and groupby_call_for_multi_agg is not None:
                    source_frame = frame_name_from_expression(groupby_call_for_multi_agg.func.value)
                    source_columns = frame_columns.get(source_frame) or {}
                    group_fields: list[str] = []
                    for argument in groupby_call_for_multi_agg.args:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            group_fields.append(str(argument.value))
                        elif isinstance(argument, (ast.List, ast.Tuple)):
                            group_fields.extend(
                                str(item.value)
                                for item in argument.elts
                                if isinstance(item, ast.Constant)
                                and isinstance(item.value, str)
                            )
                    measure_fields = list(dict.fromkeys(
                        subscript_key(item)
                        for item in ast.walk(value)
                        if isinstance(item, ast.Subscript)
                        and item.value is groupby_call_for_multi_agg
                        and subscript_key(item)
                    ))
                    aggregate_specs = [
                        str(item.value)
                        for item in groupby_multi_agg_call.args[0].elts
                        if isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                    ]
                    if source_frame and aggregate_specs:
                        operation = operation_for(node)
                        operation_id = str((operation or {}).get("operation_node_id") or "")
                        dimension_origins = [
                            merge_origins(source_columns.get(field))
                            for field in group_fields
                        ]
                        for dimension_origin in dimension_origins:
                            dimension_origin["dimension_boundaries"].update(
                                dimension_origin.get("boundaries") or set()
                            )
                        measure_origin = merge_origins(
                            *(
                                source_columns.get(field)
                                for field in measure_fields
                            ),
                            *(source_columns.values() if not measure_fields else []),
                        )
                        if measure_origin["queries"] and operation_id:
                            measure_origin["operations"].add(operation_id)
                            measure_origin["boundaries"] = {operation_id}
                        frame_columns[target] = {
                            field: dimension_origins[index]
                            for index, field in enumerate(group_fields)
                        }
                        for aggregate_name in aggregate_specs:
                            frame_columns[target][aggregate_name] = merge_origins(measure_origin)
                        frame_positions[target] = [
                            *dimension_origins,
                            *(
                                merge_origins(measure_origin)
                                for _ in aggregate_specs
                            ),
                        ]
                        if measure_origin["queries"]:
                            scalar_origins[target] = merge_origins(
                                *(frame_columns[target].values())
                            )
                        continue

                source_frame = frame_name_from_expression(value)
                row_selector = (
                    value
                    if isinstance(value, ast.Subscript)
                    and isinstance(value.value, ast.Attribute)
                    and value.value.attr in {"loc", "iloc"}
                    and frame_name_from_expression(value.value.value) == source_frame
                    else None
                )
                row_selector_returns_row = False
                if row_selector is not None:
                    selector_items = (
                        list(row_selector.slice.elts)
                        if isinstance(row_selector.slice, (ast.Tuple, ast.List))
                        else [row_selector.slice]
                    )
                    row_selector_returns_row = len(selector_items) == 1 or (
                        len(selector_items) >= 2
                        and isinstance(selector_items[1], ast.Slice)
                        and selector_items[1].lower is None
                        and selector_items[1].upper is None
                        and selector_items[1].step is None
                    )
                if source_frame and row_selector_returns_row:
                    copy_frame(target, source_frame)
                    operation = operation_for(node)
                    operation_id = str((operation or {}).get("operation_node_id") or "")
                    if operation_id:
                        for column_origin in frame_columns.get(target, {}).values():
                            if column_origin.get("queries"):
                                column_origin["operations"].add(operation_id)
                    row_origin = merge_origins(*(frame_columns.get(target) or {}).values())
                    if row_origin["queries"]:
                        scalar_origins[target] = row_origin
                    continue
                has_positional_scalar_access = any(
                    isinstance(item, ast.Subscript)
                    and isinstance(item.value, ast.Attribute)
                    and item.value.attr in {"iloc", "iat", "at"}
                    and isinstance(item.slice, ast.Constant)
                    and isinstance(item.slice.value, int)
                    for item in ast.walk(value)
                )
                is_frame_transform = bool(source_frame) and not has_positional_scalar_access and (
                    any(
                        isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr in {
                            "copy", "sort_values", "head", "tail", "nlargest", "nsmallest",
                            "dropna", "fillna", "reset_index", "rename", "groupby", "agg", "aggregate",
                            "melt", "value_counts",
                        }
                        for item in ast.walk(value)
                    )
                    or any(
                        isinstance(item, ast.Subscript)
                        and frame_name_from_expression(item.value) == source_frame
                        and not subscript_key(item)
                        and not (
                            isinstance(item.value, ast.Attribute)
                            and item.value.attr in {"loc", "iloc"}
                            and isinstance(item.slice, (ast.Tuple, ast.List))
                            and any(
                                isinstance(slice_item, ast.Constant)
                                and isinstance(slice_item.value, str)
                                for slice_item in item.slice.elts[1:]
                            )
                        )
                        for item in ast.walk(value)
                    )
                )
                if is_frame_transform:
                    melt_call = next(
                        (
                            item for item in ast.walk(value)
                            if isinstance(item, ast.Call)
                            and isinstance(item.func, ast.Attribute)
                            and item.func.attr == "melt"
                        ),
                        None,
                    )
                    if melt_call is not None:
                        def melt_string_values(keyword_name: str) -> list[str]:
                            keyword_value = next(
                                (
                                    keyword.value for keyword in melt_call.keywords
                                    if keyword.arg == keyword_name
                                ),
                                None,
                            )
                            if isinstance(keyword_value, ast.Constant) and isinstance(keyword_value.value, str):
                                return [str(keyword_value.value)]
                            if isinstance(keyword_value, (ast.List, ast.Tuple)):
                                return [
                                    str(item.value)
                                    for item in keyword_value.elts
                                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                                ]
                            return []

                        def melt_string_value(keyword_name: str, default: str) -> str:
                            values = melt_string_values(keyword_name)
                            return values[0] if values else default

                        source_columns = frame_columns.get(source_frame) or {}
                        id_fields = melt_string_values("id_vars")
                        value_fields = melt_string_values("value_vars") or [
                            field for field in source_columns if field not in id_fields
                        ]
                        variable_field = melt_string_value("var_name", "variable")
                        value_field = melt_string_value("value_name", "value")
                        variable_origin = merge_origins(
                            *(source_columns.get(field) for field in value_fields)
                        )
                        variable_origin["dimension_boundaries"].update(
                            variable_origin.get("boundaries") or set()
                        )
                        value_origin = merge_origins(
                            *(source_columns.get(field) for field in value_fields)
                        )
                        operation = operation_for(node)
                        operation_id = str((operation or {}).get("operation_node_id") or "")
                        if operation_id and value_origin["queries"]:
                            value_origin["operations"].add(operation_id)
                            variable_origin["operations"].add(operation_id)
                        frame_columns[target] = {
                            field: merge_origins(source_columns.get(field))
                            for field in id_fields
                            if field in source_columns
                        }
                        frame_columns[target][variable_field] = variable_origin
                        frame_columns[target][value_field] = value_origin
                        frame_positions[target] = [
                            frame_columns[target][field]
                            for field in frame_columns[target]
                        ]
                        if value_origin["queries"]:
                            scalar_origins[target] = merge_origins(
                                *(frame_columns[target].values())
                            )
                        continue

                    value_counts_call = next(
                        (
                            item for item in ast.walk(value)
                            if isinstance(item, ast.Call)
                            and isinstance(item.func, ast.Attribute)
                            and item.func.attr == "value_counts"
                        ),
                        None,
                    )
                    if value_counts_call is not None:
                        dimension_field = next(
                            (
                                subscript_key(item)
                                for item in ast.walk(value_counts_call.func.value)
                                if isinstance(item, ast.Subscript)
                                and frame_name_from_expression(item.value) == source_frame
                                and subscript_key(item)
                            ),
                            "",
                        )
                        dimension_origin = merge_origins(
                            (frame_columns.get(source_frame) or {}).get(dimension_field)
                        )
                        dimension_origin["dimension_boundaries"].update(
                            dimension_origin.get("boundaries") or set()
                        )
                        aggregate_origin = merge_origins(dimension_origin)
                        aggregate_origin["dimension_boundaries"].clear()
                        operation = operation_for(node)
                        operation_id = str(
                            (operation or {}).get("operation_node_id") or ""
                        )
                        if aggregate_origin["queries"] and operation_id:
                            aggregate_origin["operations"].add(operation_id)
                            aggregate_origin["boundaries"] = {operation_id}
                        reset_index_name = next(
                            (
                                str(keyword.value.value)
                                for call in ast.walk(value)
                                if isinstance(call, ast.Call)
                                and isinstance(call.func, ast.Attribute)
                                and call.func.attr == "reset_index"
                                for keyword in call.keywords
                                if keyword.arg == "name"
                                and isinstance(keyword.value, ast.Constant)
                                and isinstance(keyword.value.value, str)
                            ),
                            "",
                        )
                        measure_field = reset_index_name or "__value_counts__"
                        frame_columns[target] = {
                            dimension_field: dimension_origin,
                            measure_field: aggregate_origin,
                        }
                        frame_positions[target] = [
                            dimension_origin,
                            aggregate_origin,
                        ]
                        if aggregate_origin["queries"]:
                            scalar_origins[target] = aggregate_origin
                        continue

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
                    operation = operation_for(node)
                    groupby_call = next(
                        (
                            item for item in ast.walk(value)
                            if isinstance(item, ast.Call)
                            and isinstance(item.func, ast.Attribute)
                            and item.func.attr == "groupby"
                        ),
                        None,
                    )
                    if (
                        operation
                        and operation.get("semantic_kind") == "aggregation"
                        and groupby_call is not None
                    ):
                        aggregate_origin = merge_origins(
                            *(frame_columns.get(source_frame) or {}).values()
                        )
                        operation_id = str(operation.get("operation_node_id") or "")
                        if aggregate_origin["queries"] and operation_id:
                            aggregate_origin["operations"].add(operation_id)
                            aggregate_origin["boundaries"] = {operation_id}
                            group_fields: list[str] = []
                            for argument in groupby_call.args:
                                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                                    group_fields.append(str(argument.value))
                                elif isinstance(argument, (ast.List, ast.Tuple)):
                                    group_fields.extend(
                                        str(item.value)
                                        for item in argument.elts
                                        if isinstance(item, ast.Constant)
                                        and isinstance(item.value, str)
                                    )
                            dimension_origins = [
                                merge_origins((frame_columns.get(source_frame) or {}).get(field))
                                for field in group_fields
                            ]
                            for dimension_origin in dimension_origins:
                                dimension_origin["dimension_boundaries"].update(
                                    dimension_origin.get("boundaries") or set()
                                )
                            aggregate_fields = list(dict.fromkeys(
                                subscript_key(item)
                                for item in ast.walk(value)
                                if isinstance(item, ast.Subscript)
                                and frame_name_from_expression(item.value) == source_frame
                                and subscript_key(item)
                                and subscript_key(item) not in group_fields
                            ))
                            aggregate_field_origins: dict[str, dict[str, set[str]]] = {}
                            for field in aggregate_fields:
                                field_origin = merge_origins(
                                    (frame_columns.get(source_frame) or {}).get(field)
                                )
                                if not field_origin["queries"]:
                                    field_origin = merge_origins(aggregate_origin)
                                field_origin["dimension_boundaries"].clear()
                                if field_origin["queries"] and operation_id:
                                    field_origin["operations"].add(operation_id)
                                    field_origin["boundaries"] = {operation_id}
                                aggregate_field_origins[field] = field_origin
                            named_aggregate_origins: dict[str, dict[str, set[str]]] = {}
                            aggregate_call = next(
                                (
                                    item for item in ast.walk(value)
                                    if isinstance(item, ast.Call)
                                    and isinstance(item.func, ast.Attribute)
                                    and item.func.attr in {"agg", "aggregate"}
                                    and item.keywords
                                ),
                                None,
                            )
                            if aggregate_call is not None:
                                for keyword in aggregate_call.keywords:
                                    if not keyword.arg or not isinstance(keyword.value, (ast.Tuple, ast.List)):
                                        continue
                                    elements = list(keyword.value.elts)
                                    if len(elements) != 2:
                                        continue
                                    source_field_node, aggregation_node = elements
                                    source_fields = [
                                        str(item.value)
                                        for item in ast.walk(source_field_node)
                                        if isinstance(item, ast.Constant)
                                        and isinstance(item.value, str)
                                    ]
                                    if not source_fields or not (
                                        isinstance(aggregation_node, ast.Constant)
                                        or isinstance(aggregation_node, (ast.Name, ast.Attribute, ast.Lambda))
                                    ):
                                        continue
                                    source_origin = merge_origins(
                                        *(
                                            (frame_columns.get(source_frame) or {}).get(field)
                                            for field in source_fields
                                        )
                                    )
                                    if not source_origin["queries"]:
                                        continue
                                    source_origin["dimension_boundaries"].clear()
                                    source_origin["operations"].add(operation_id)
                                    source_origin["boundaries"] = {operation_id}
                                    named_aggregate_origins[str(keyword.arg)] = source_origin
                            if group_fields:
                                frame_columns[target] = {
                                    field: dimension_origins[index]
                                    for index, field in enumerate(group_fields)
                                }
                                for field in aggregate_fields:
                                    frame_columns[target][field] = aggregate_field_origins[field]
                                frame_columns[target].update(named_aggregate_origins)
                                metric_positions = [
                                    *(frame_columns[target][field] for field in aggregate_fields),
                                    *named_aggregate_origins.values(),
                                ] or [aggregate_origin]
                                if not aggregate_fields and not named_aggregate_origins:
                                    aggregate_call = next(
                                        (
                                            item for item in ast.walk(value)
                                            if isinstance(item, ast.Call)
                                            and isinstance(item.func, ast.Attribute)
                                            and item.func.attr in {"agg", "aggregate"}
                                        ),
                                        None,
                                    )
                                    aggregate_specs: list[str] = []
                                    if (
                                        aggregate_call is not None
                                        and aggregate_call.args
                                        and isinstance(aggregate_call.args[0], (ast.List, ast.Tuple))
                                    ):
                                        aggregate_specs = [
                                            str(item.value)
                                            for item in aggregate_call.args[0].elts
                                            if isinstance(item, ast.Constant)
                                            and isinstance(item.value, str)
                                        ]
                                    if aggregate_specs:
                                        metric_positions = [
                                            merge_origins(aggregate_origin)
                                            for _ in aggregate_specs
                                        ]
                                frame_positions[target] = [
                                    *dimension_origins,
                                    *metric_positions,
                                ]
                            reset_index_name = next(
                                (
                                    str(keyword.value.value)
                                    for call in ast.walk(value)
                                    if isinstance(call, ast.Call)
                                    and isinstance(call.func, ast.Attribute)
                                    and call.func.attr == "reset_index"
                                    for keyword in call.keywords
                                    if keyword.arg == "name"
                                    and isinstance(keyword.value, ast.Constant)
                                    and isinstance(keyword.value.value, str)
                                ),
                                "",
                            )
                            if reset_index_name:
                                frame_columns.setdefault(target, {})[
                                    reset_index_name
                                ] = aggregate_origin
                                if not group_fields:
                                    frame_positions[target] = [
                                        frame_columns[target][field]
                                        for field in frame_columns[target]
                                    ]
                    frame_origin = merge_origins(
                        *(frame_columns.get(target) or {}).values()
                    )
                    if operation and frame_origin["queries"]:
                        operation_id = str(operation.get("operation_node_id") or "")
                        if operation_id:
                            frame_origin["operations"].add(operation_id)
                            frame_origin["boundaries"] = {operation_id}
                            if operation.get("semantic_kind") == "ranking":
                                # Sorting and Top-N constrain which rows are shown,
                                # but they do not replace each column's metric
                                # calculation boundary. Keep the row-scope operation
                                # in the chain while preserving upstream aggregation
                                # or formula boundaries per column.
                                for column_origin in (frame_columns.get(target) or {}).values():
                                    if not column_origin.get("queries"):
                                        continue
                                    column_origin["operations"].add(operation_id)
                    if frame_origin["queries"]:
                        scalar_origins[target] = frame_origin
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
                indexed_frame_name = ""
                indexed_field = ""
                if (
                    isinstance(target_node, ast.Subscript)
                    and isinstance(target_node.value, ast.Attribute)
                    and target_node.value.attr in {"at", "loc", "iat", "iloc"}
                    and isinstance(target_node.value.value, ast.Name)
                ):
                    slice_items = (
                        list(target_node.slice.elts)
                        if isinstance(target_node.slice, (ast.Tuple, ast.List))
                        else [target_node.slice]
                    )
                    indexed_frame_name = target_node.value.value.id
                    indexed_field = next(
                        (
                            str(item.value)
                            for item in reversed(slice_items)
                            if isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                        ),
                        "",
                    )
                if indexed_frame_name and indexed_field:
                    origin = expression_origin(value, line)
                    operation = operation_for(node)
                    if operation and origin["queries"]:
                        operation_id = str(operation.get("operation_node_id") or "")
                        origin["operations"].add(operation_id)
                        if operation.get("semantic_kind") in {
                            "aggregation", "formula", "ranking", "projection",
                        }:
                            origin["boundaries"] = {operation_id}
                    if origin["queries"]:
                        existing = (frame_columns.get(indexed_frame_name) or {}).get(
                            indexed_field
                        )
                        frame_columns.setdefault(indexed_frame_name, {})[
                            indexed_field
                        ] = merge_origins(existing, origin)
                        frame_positions[indexed_frame_name] = [
                            frame_columns[indexed_frame_name][name]
                            for name in frame_columns[indexed_frame_name]
                        ]
                elif isinstance(target_node, ast.Subscript) and isinstance(target_node.value, ast.Name):
                    frame_name = target_node.value.id
                    field = subscript_key(target_node)
                    if not field:
                        if isinstance(value, ast.Dict):
                            for key_node, value_node in zip(value.keys, value.values):
                                if not (
                                    isinstance(key_node, ast.Constant)
                                    and isinstance(key_node.value, str)
                                ):
                                    continue
                                field_origin = expression_origin(value_node, line)
                                if field_origin.get("queries"):
                                    mapping_field_origins.setdefault(frame_name, {})[
                                        str(key_node.value)
                                    ] = field_origin
                        origin = expression_origin(value, line)
                        if origin.get("queries"):
                            mapping_value_origins[frame_name] = merge_origins(
                                mapping_value_origins.get(frame_name),
                                origin,
                            )
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

        # Propagate lineage through ordinary Python sequence iteration. DataFrame
        # row iterators are handled by ``row_frames``; this covers sorted mappings
        # and other named sequences unpacked by ``for`` or ``enumerate``.
        for loop in (item for item in ast.walk(tree) if isinstance(item, ast.For)):
            iterator = loop.iter
            ordinal_name = ""
            if (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Name)
                and iterator.func.id == "enumerate"
                and iterator.args
            ):
                iterator = iterator.args[0]
                if isinstance(loop.target, (ast.Tuple, ast.List)) and loop.target.elts:
                    ordinal_target = loop.target.elts[0]
                    if isinstance(ordinal_target, ast.Name):
                        ordinal_name = ordinal_target.id
            if not isinstance(iterator, ast.Name):
                continue
            origin = scalar_origins.get(iterator.id)
            if not origin or not origin.get("queries"):
                continue
            start = int(getattr(loop, "lineno", 0) or 0)
            end = int(getattr(loop, "end_lineno", start) or start)
            for target_name in _target_names(loop.target):
                if target_name in {"_", ordinal_name}:
                    continue
                iteration_alias_scopes.append((
                    target_name,
                    start,
                    end,
                    merge_origins(origin),
                ))

        # ``build_table_result`` accepts a list of record dictionaries directly.
        # Resolve these containers after frame assignments so row expressions can
        # inherit the completed upstream DataFrame lineage.
        appended_record_rows: dict[str, list[dict[str, Any]]] = {}
        for container_name, records in appended_record_dicts.items():
            column_order: list[str] = []
            column_origins: dict[str, dict[str, set[str]]] = {}
            record_rows: list[dict[str, Any]] = []
            for record in records:
                line = int(getattr(record, "lineno", 0) or 0)
                record_values: dict[str, Any] = {}
                record_origins: dict[str, dict[str, set[str]]] = {}
                for key_node, value_node in zip(record.keys, record.values):
                    field = static_string(key_node)
                    if not field:
                        continue
                    if isinstance(value_node, ast.Constant) and isinstance(
                        value_node.value, (str, int, float, bool)
                    ):
                        record_values[field] = value_node.value
                    if field not in column_order:
                        column_order.append(field)
                    row_origin = expression_origin(value_node, line)
                    if not row_origin.get("queries"):
                        direct_operation_ids = list(dict.fromkeys(
                            operation_id
                            for loaded_name in _loaded_names(value_node)
                            for operation_id in operation_producers.get(
                                str(loaded_name),
                                set(),
                            )
                        ))
                        boundary_ids = cls._nearest_calculation_boundaries(
                            direct_operation_ids,
                            operation_by_id,
                            operation_producer_lists,
                        )
                        query_ids = cls._query_dependencies(
                            boundary_ids or direct_operation_ids,
                            operation_by_id,
                            operation_producer_lists,
                        )
                        if query_ids and boundary_ids:
                            row_origin["queries"].update(query_ids)
                            row_origin["boundaries"].update(boundary_ids)
                            row_origin["operations"].update(
                                [*direct_operation_ids, *boundary_ids]
                            )
                    field_origin = merge_origins(
                        column_origins.get(field),
                        row_origin,
                    )
                    write_target = f"{container_name}[{field!r}]"
                    write_operation = next(
                        (
                            operation
                            for operation in operations_by_line.get(line, [])
                            if write_target in (operation.get("targets") or [])
                            and operation.get("semantic_kind")
                            in {"aggregation", "formula", "ranking"}
                        ),
                        None,
                    )
                    if field_origin.get("queries") and write_operation is not None:
                        operation_id = str(write_operation.get("operation_node_id") or "")
                        if operation_id:
                            field_origin["operations"].add(operation_id)
                            field_origin["boundaries"] = {operation_id}
                    column_origins[field] = field_origin
                    record_origins[field] = row_origin
                if record_values and record_origins:
                    record_rows.append({
                        "values": record_values,
                        "origins": record_origins,
                    })
            if column_origins:
                frame_columns[container_name] = column_origins
                frame_positions[container_name] = [
                    column_origins[field] for field in column_order
                ]
            if record_rows:
                appended_record_rows[container_name] = record_rows

        # A record-shaped table can expose one numeric column whose rows come
        # from independent calculations. Preserve each row boundary behind a
        # stable business selector instead of collapsing the column into one
        # EvidenceDefinition.
        def scoped_origins_for_records(
            records: list[dict[str, Any]],
        ) -> dict[str, list[dict[str, Any]]]:
            result: dict[str, list[dict[str, Any]]] = {}
            if len(records) < 2:
                return result
            metric_fields = set().union(*(
                set(item.get("origins") or {}) for item in records
            ))
            for metric_field in metric_fields:
                scoped_rows = [
                    item for item in records
                    if (item.get("origins") or {}).get(metric_field, {}).get("queries")
                    and (item.get("origins") or {}).get(metric_field, {}).get("boundaries")
                ]
                if len(scoped_rows) != len(records):
                    continue
                selector_fields = [
                    key for key in (scoped_rows[0].get("values") or {})
                    if key != metric_field
                    and all(key in (item.get("values") or {}) for item in scoped_rows)
                    and len({
                        _stable_json((item.get("values") or {}).get(key))
                        for item in scoped_rows
                    }) == len(scoped_rows)
                ]
                if not selector_fields:
                    continue
                selector_field = selector_fields[0]
                result[metric_field] = [
                    {
                        "row_selector": {
                            selector_field: (item.get("values") or {}).get(selector_field),
                        },
                        "origin": (item.get("origins") or {}).get(metric_field) or {},
                    }
                    for item in scoped_rows
                ]
            return result

        scoped_record_origins: dict[str, dict[str, list[dict[str, Any]]]] = {}
        record_rows_by_container = {
            **appended_record_rows,
            **literal_dataframe_record_rows,
        }
        scoped_record_frames = {
            container_name: container_name
            for container_name in record_rows_by_container
        }
        scoped_record_frames.update(record_table_frames)
        for frame_name, container_name in scoped_record_frames.items():
            scoped = scoped_origins_for_records(
                record_rows_by_container.get(container_name) or []
            )
            if scoped:
                scoped_record_origins[frame_name] = scoped

        # A directly declared record table has no shared source frame: every
        # column value is an independent expression and can cross a different
        # query/calculation boundary. Keep those origins by artifact and field
        # instead of assigning the whole table to the first loaded scalar.
        literal_table_origins: list[dict[str, dict[str, set[str]]]] = []
        literal_table_scoped_origins: list[
            dict[str, list[dict[str, Any]]]
        ] = []
        for table_expression in cls._table_artifact_expressions(tree):
            rows_expression = cls._table_rows_expression(table_expression)
            records = (
                list(rows_expression.elts)
                if isinstance(rows_expression, (ast.List, ast.Tuple))
                and all(isinstance(item, ast.Dict) for item in rows_expression.elts)
                else [rows_expression]
                if isinstance(rows_expression, ast.Dict)
                else []
            )
            column_origins: dict[str, dict[str, set[str]]] = {}
            record_rows: list[dict[str, Any]] = []
            for record in records:
                if not isinstance(record, ast.Dict):
                    continue
                record_values: dict[str, Any] = {}
                record_origins: dict[str, dict[str, set[str]]] = {}
                for key_node, value_node in zip(record.keys, record.values):
                    field = static_string(key_node)
                    if not field:
                        continue
                    if isinstance(value_node, ast.Constant) and isinstance(
                        value_node.value, (str, int, float, bool)
                    ):
                        record_values[field] = value_node.value
                    value_line = int(getattr(value_node, "lineno", 0) or 0)
                    value_origin = expression_origin(value_node, value_line)
                    column_origins[field] = merge_origins(
                        column_origins.get(field), value_origin
                    )
                    record_origins[field] = value_origin
                if record_values and record_origins:
                    record_rows.append({
                        "values": record_values,
                        "origins": record_origins,
                    })
            literal_table_origins.append(column_origins)
            literal_table_scoped_origins.append(
                scoped_origins_for_records(record_rows)
            )

        enriched: list[dict[str, Any]] = []
        def local_calculation_ancestry(operation_id: str) -> set[str]:
            pending = [operation_id]
            visited: set[str] = set()
            local: set[str] = set()
            while pending:
                current_id = pending.pop()
                if current_id in visited:
                    continue
                visited.add(current_id)
                current = operation_by_id.get(current_id) or {}
                if (
                    current.get("kind") not in {
                        "query_load", "query_projection", "source_field",
                        "validated_query", "output_expression",
                    }
                    and current.get("semantic_kind")
                    in {"aggregation", "formula", "ranking", "projection"}
                ):
                    local.add(current_id)
                for loaded_name in current.get("loads") or []:
                    pending.extend(operation_producers.get(str(loaded_name), set()))
            return local

        def count_derived_measure(operation_id: str, seen: set[str] | None = None) -> bool:
            seen = set(seen or ())
            if operation_id in seen:
                return True
            seen.add(operation_id)
            operation = operation_by_id.get(operation_id) or {}
            source = str(operation.get("source") or "")
            semantic_kind = str(operation.get("semantic_kind") or "")
            called: set[str] = set()
            parsed: ast.AST | None = None
            try:
                parsed = ast.parse(source)
                called = _called_operation_names(parsed)
            except SyntaxError:
                parsed = None
            if semantic_kind == "aggregation":
                boolean_predicate_methods = {
                    "between", "eq", "ge", "gt", "isin", "isna",
                    "le", "lt", "ne", "notna",
                }

                def is_boolean_count_sum(item: ast.AST) -> bool:
                    if not (
                        isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr == "sum"
                    ):
                        return False
                    receiver = item.func.value
                    return any(
                        isinstance(predicate, (ast.Compare, ast.BoolOp))
                        or (
                            isinstance(predicate, ast.Call)
                            and isinstance(predicate.func, ast.Attribute)
                            and predicate.func.attr in boolean_predicate_methods
                        )
                        for predicate in ast.walk(receiver)
                    )

                boolean_sum = bool(
                    parsed is not None
                    and any(is_boolean_count_sum(item) for item in ast.walk(parsed))
                )
                return bool(called.intersection({
                    "len", "count", "size", "nunique", "value_counts",
                })) or boolean_sum or bool(
                    re.search(r"\bCOUNT\s*\(", source, re.IGNORECASE)
                )
            if parsed is None:
                return False
            parent_ids = {
                parent_id
                for loaded_name in operation.get("loads") or []
                for parent_id in operation_producers.get(str(loaded_name), set())
                if parent_id != operation_id
                and (operation_by_id.get(parent_id) or {}).get("kind")
                != "output_expression"
                and int((operation_by_id.get(parent_id) or {}).get("line") or 0)
                <= int(operation.get("line") or 0)
            }
            # Selecting one category from a value_counts/count mapping does
            # not change the measure.  Preserve that shared count family for
            # ``int(counts.get(...))`` and equivalent scalar subscriptions so
            # category partitions can remain one chart value field.
            scalar_selection = (
                semantic_kind == "transformation"
                and bool(parent_ids)
                and (
                    "get" in called
                    or any(isinstance(item, ast.Subscript) for item in ast.walk(parsed))
                )
                and not any(isinstance(item, ast.BinOp) for item in ast.walk(parsed))
            )
            if scalar_selection:
                return all(
                    count_derived_measure(parent_id, seen)
                    for parent_id in parent_ids
                )
            if semantic_kind != "formula":
                return False
            # A remainder such as ``total - active - expired`` is still a
            # category count. Ratios and weighted scores are different units
            # and must not be folded into that count family.
            if any(
                isinstance(item, ast.BinOp)
                and not isinstance(item.op, (ast.Add, ast.Sub))
                for item in ast.walk(parsed)
            ):
                return False
            return bool(parent_ids) and all(
                count_derived_measure(parent_id, seen)
                for parent_id in parent_ids
            )

        def apply_structured_origin(
            output: dict[str, Any],
            origin: dict[str, set[str]],
        ) -> dict[str, Any]:
            result = dict(output)
            boundary_ids = sorted(origin.get("boundaries") or [])
            result["structured_origin"] = True
            result["candidate_query_node_ids"] = sorted(origin.get("queries") or [])
            result["boundary_operation_node_ids"] = boundary_ids
            result["candidate_operation_node_ids"] = sorted(
                set(origin.get("operations") or set()) | set(boundary_ids)
            )
            result["direct_operation_node_ids"] = boundary_ids
            source_fields = sorted(origin.get("fields") or [])
            if source_fields and (
                result.get("point_selector") or result.get("row_selector")
            ):
                result["lineage_fields"] = source_fields
            result["structured_dimension_origin"] = bool(
                set(boundary_ids).intersection(origin.get("dimension_boundaries") or set())
            )
            result["ambiguous_calculation_boundary"] = False
            return result

        for raw_output in outputs:
            output = dict(raw_output)
            source_frame = str(output.get("source_frame") or "").strip()
            scoped_origins = (
                scoped_record_origins.get(source_frame, {}).get(
                    str(output.get("field") or "")
                )
                if output.get("artifact_type") in {"chart", "table"}
                else None
            )
            if not scoped_origins and output.get("artifact_type") == "chart":
                selector_field = str(
                    output.get("_structured_selector_field") or ""
                ).strip()
                selector_expressions = output.get(
                    "_structured_selector_expressions"
                ) or []
                value_expressions = output.get(
                    "_structured_value_expressions"
                ) or []
                if (
                    selector_field
                    and len(selector_expressions) >= 2
                    and len(selector_expressions) == len(value_expressions)
                ):
                    candidate_scopes: list[dict[str, Any]] = []
                    for selector_expression, value_expression in zip(
                        selector_expressions,
                        value_expressions,
                    ):
                        if not (
                            isinstance(selector_expression, ast.Constant)
                            and isinstance(
                                selector_expression.value,
                                (str, int, float, bool),
                            )
                        ):
                            candidate_scopes = []
                            break
                        resolved_value_expression = (
                            assignment_values.get(value_expression.id, value_expression)
                            if isinstance(value_expression, ast.Name)
                            else value_expression
                        )
                        value_origin = expression_origin(
                            resolved_value_expression,
                            int(getattr(resolved_value_expression, "lineno", 0) or 0),
                        )
                        if not value_origin.get("queries"):
                            direct_operation_ids = list(dict.fromkeys(
                                operation_id
                                for loaded_name in _loaded_names(value_expression)
                                for operation_id in operation_producers.get(
                                    str(loaded_name),
                                    set(),
                                )
                            ))
                            boundary_ids = cls._nearest_calculation_boundaries(
                                direct_operation_ids,
                                operation_by_id,
                                operation_producer_lists,
                            )
                            query_ids = cls._query_dependencies(
                                boundary_ids or direct_operation_ids,
                                operation_by_id,
                                operation_producer_lists,
                            )
                            if query_ids and boundary_ids:
                                value_origin["queries"].update(query_ids)
                                value_origin["boundaries"].update(boundary_ids)
                                value_origin["operations"].update([
                                    *direct_operation_ids,
                                    *boundary_ids,
                                ])
                        if not value_origin.get("queries"):
                            referenced_fields = {
                                str(item.slice.value)
                                for item in ast.walk(resolved_value_expression)
                                if isinstance(item, ast.Subscript)
                                and isinstance(item.slice, ast.Constant)
                                and isinstance(item.slice.value, str)
                            }
                            projection_operations = [
                                operation
                                for operation in operation_by_id.values()
                                if operation.get("kind") == "query_projection"
                                and referenced_fields.intersection(
                                    str(target)
                                    for target in operation.get("targets") or []
                                )
                                and str(operation.get("query_node_id") or "")
                            ]
                            direct_operation_ids = list(dict.fromkeys(
                                operation_id
                                for loaded_name in _loaded_names(value_expression)
                                for operation_id in operation_producers.get(
                                    str(loaded_name),
                                    set(),
                                )
                            ))
                            boundary_ids = cls._nearest_calculation_boundaries(
                                direct_operation_ids,
                                operation_by_id,
                                operation_producer_lists,
                            )
                            if projection_operations and boundary_ids:
                                value_origin["queries"].update(
                                    str(operation.get("query_node_id") or "")
                                    for operation in projection_operations
                                )
                                value_origin["boundaries"].update(boundary_ids)
                                value_origin["operations"].update([
                                    *direct_operation_ids,
                                    *boundary_ids,
                                    *[
                                        str(operation.get("operation_node_id") or "")
                                        for operation in projection_operations
                                    ],
                                ])
                        if not (
                            value_origin.get("queries")
                            and value_origin.get("boundaries")
                        ):
                            candidate_scopes = []
                            break
                        candidate_scopes.append({
                            "row_selector": {
                                selector_field: selector_expression.value,
                            },
                            "origin": value_origin,
                        })
                    if len(candidate_scopes) == len(value_expressions):
                        scoped_origins = candidate_scopes
            if (
                not scoped_origins
                and output.get("artifact_type") == "table"
            ):
                artifact_index = int(output.get("artifact_index") or 0)
                if 0 <= artifact_index < len(literal_table_scoped_origins):
                    scoped_origins = literal_table_scoped_origins[
                        artifact_index
                    ].get(str(output.get("field") or ""))
            if scoped_origins and output.get("artifact_type") == "chart":
                scoped_boundary_ids = sorted({
                    str(boundary_id)
                    for item in scoped_origins
                    for boundary_id in (item.get("origin") or {}).get("boundaries") or []
                    if str(boundary_id)
                })
                families = {
                    cls._operation_family_signature(operation_by_id.get(item) or {})
                    for item in scoped_boundary_ids
                }
                shared_local_boundary = bool(scoped_boundary_ids) and bool(
                    set.intersection(*(
                        local_calculation_ancestry(item)
                        for item in scoped_boundary_ids
                    ))
                )
                shared_count_measure = bool(scoped_boundary_ids) and all(
                    count_derived_measure(item)
                    for item in scoped_boundary_ids
                )
                write_signatures = {
                    signature
                    for item in scoped_boundary_ids
                    if (
                        signature := cls._operation_write_signature(
                            operation_by_id.get(item) or {}
                        )
                    )
                }
                shared_write_target = len(write_signatures) == 1
                scope_field_sets = {
                    tuple(sorted(
                        str(field)
                        for field in (item.get("origin") or {}).get("fields") or []
                        if str(field)
                    ))
                    for item in scoped_origins
                }
                distinct_source_fields = (
                    len(scope_field_sets) > 1
                    and all(scope_field_sets)
                )
                requires_point_boundaries = (
                    len(families) > 1
                    and not shared_count_measure
                    and (
                        distinct_source_fields
                        or (
                            not shared_local_boundary
                            and not shared_write_target
                        )
                    )
                )
                if not requires_point_boundaries:
                    scoped_origins = None
            if scoped_origins:
                structured_rows = (
                    output.get("_table_rows")
                    or output.get("_structured_rows")
                    or []
                )
                matching_scopes = (
                    [
                        item for item in scoped_origins
                        if any(
                            all(row.get(key) == value for key, value in (item.get("row_selector") or {}).items())
                            for row in structured_rows
                            if isinstance(row, dict)
                        )
                    ]
                    if structured_rows
                    else list(scoped_origins)
                )
                if matching_scopes:
                    for item in matching_scopes:
                        selector = item.get("row_selector") or {}
                        scoped_output = dict(output)
                        scoped_output.pop("_table_rows", None)
                        scoped_output.pop("_structured_rows", None)
                        scoped_output.pop("_structured_selector_field", None)
                        scoped_output.pop("_structured_selector_expressions", None)
                        if output.get("artifact_type") == "chart":
                            scoped_output["point_selector"] = selector
                        else:
                            scoped_output["row_selector"] = selector
                        scoped_output["output_node_id"] = (
                            f"{output.get('output_node_id')}__scope_{_digest(_stable_json(selector), 10)}"
                        )
                        enriched.append(apply_structured_origin(
                            scoped_output,
                            item.get("origin") or {},
                        ))
                    continue
            output.pop("_table_rows", None)
            output.pop("_structured_rows", None)
            output.pop("_structured_selector_field", None)
            output.pop("_structured_selector_expressions", None)
            structured_value_expressions = output.pop(
                "_structured_value_expressions",
                [],
            )
            origin: dict[str, set[str]] | None = (
                merge_origins(*(
                    expression_origin(
                        expression,
                        int(getattr(expression, "lineno", 0) or 0),
                    )
                    for expression in structured_value_expressions
                    if isinstance(expression, ast.AST)
                ))
                if structured_value_expressions
                else None
            )
            needs_origin = not (
                origin
                and origin.get("queries")
                and origin.get("boundaries")
            )
            if needs_origin and source_frame:
                lineage_fields = [
                    str(field).strip()
                    for field in output.get("lineage_fields") or []
                    if str(field).strip()
                ]
                field_candidates = list(dict.fromkeys([
                    *lineage_fields,
                    str(output.get("field") or "").strip(),
                ]))
                origin = next(
                    (
                        (frame_columns.get(source_frame) or {}).get(field)
                        for field in field_candidates
                        if (frame_columns.get(source_frame) or {}).get(field) is not None
                    ),
                    None,
                )
                if origin is None and output.get("artifact_type") == "table":
                    position = int(output.get("column_index") or 0)
                    positions = frame_positions.get(source_frame) or []
                    if 0 <= position < len(positions):
                        origin = positions[position]
            elif needs_origin and output.get("artifact_type") == "table":
                table_index = int(output.get("artifact_index") or 0)
                if 0 <= table_index < len(literal_table_origins):
                    origin = literal_table_origins[table_index].get(
                        str(output.get("field") or "")
                    )
            elif needs_origin and output.get("artifact_type") == "report":
                try:
                    expression = ast.parse(str(output.get("field") or ""), mode="eval")
                    origin = expression_origin(expression, int(output.get("source_line") or 0))
                    loaded_names = {
                        item.id
                        for item in ast.walk(expression)
                        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
                    }
                    if is_presentation_ordinal(
                        loaded_names,
                        int(output.get("source_line") or 0),
                    ):
                        output["presentation_ordinal"] = True
                    expression_source = str(output.get("field") or "").strip()
                    expression_operation = next(
                        (
                            item for item in operations
                            if item.get("kind") == "output_expression"
                            and str(item.get("source") or "").strip() == expression_source
                            and int(item.get("line") or 0) == int(output.get("source_line") or 0)
                        ),
                        None,
                    )
                    if (
                        origin
                        and origin.get("queries")
                        and expression_operation
                        and expression_operation.get("semantic_kind")
                        in {"aggregation", "formula", "ranking"}
                    ):
                        operation_id = str(expression_operation.get("operation_node_id") or "")
                        origin["operations"].add(operation_id)
                        origin["boundaries"] = {operation_id}
                except SyntaxError:
                    origin = None
            if not (
                origin
                and origin.get("queries")
                and origin.get("boundaries")
            ) and output.get("artifact_type") == "table":
                runtime_query_ids = [
                    str(item)
                    for item in output.get("runtime_row_query_node_ids") or []
                    if str(item)
                ]
                sample_values = output.get("sample_values") or []
                row_count_operations = [
                    query_row_count_operations.get(query_id)
                    for query_id in runtime_query_ids
                ]
                if (
                    runtime_query_ids
                    and len(runtime_query_ids) == len(sample_values)
                    and all(row_count_operations)
                    and all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and float(value) == float(operation.get("observed_value"))
                        for value, operation in zip(
                            sample_values,
                            row_count_operations,
                        )
                    )
                ):
                    operation_ids = {
                        str(operation.get("operation_node_id") or "")
                        for operation in row_count_operations
                        if str(operation.get("operation_node_id") or "")
                    }
                    origin = {
                        "queries": set(runtime_query_ids),
                        "operations": operation_ids,
                        "boundaries": operation_ids,
                        "dimension_boundaries": set(),
                    }
                    output["lineage_fields"] = ["query_result_row_count"]
            if origin and origin.get("queries") and origin.get("boundaries"):
                boundary_ids = sorted(origin["boundaries"])
                families = {
                    cls._operation_family_signature(operation_by_id.get(item) or {})
                    for item in boundary_ids
                }
                output = apply_structured_origin(output, origin)
                shared_local_boundary = bool(boundary_ids) and bool(
                    set.intersection(*(
                        local_calculation_ancestry(item)
                        for item in boundary_ids
                    ))
                )
                shared_count_measure = bool(boundary_ids) and all(
                    count_derived_measure(item)
                    for item in boundary_ids
                )
                write_signatures = {
                    signature
                    for item in boundary_ids
                    if (
                        signature := cls._operation_write_signature(
                            operation_by_id.get(item) or {}
                        )
                    )
                }
                shared_write_target = len(write_signatures) == 1
                # Different formulas can still be one coherent parameterized
                # metric when every branch descends from the same local
                # calculation (for example a root-cause score whose formula
                # varies by cause category), or when mutually exclusive branches
                # populate the same derived field. Unrelated query projections
                # such as amount/profit/count stay deliberately ambiguous.
                output["ambiguous_calculation_boundary"] = (
                    len(families) > 1
                    and not shared_local_boundary
                    and not shared_count_measure
                    and not shared_write_target
                )
                output["heterogeneous_structured_value"] = bool(
                    output["ambiguous_calculation_boundary"]
                )
            enriched.append(output)

        return enriched

    @staticmethod
    def _is_known_dimension_output(
        output: dict[str, Any],
        datasource_snapshot: list[dict[str, Any]],
    ) -> bool:
        expression = str(output.get("field") or "").strip()
        if expression:
            try:
                expression_tree = ast.parse(expression, mode="eval")
            except SyntaxError:
                expression_tree = None
            if expression_tree is not None and any(
                isinstance(node, ast.Call)
                and LineageIndexer._numeric_call_name(node)
                in {
                    "count", "len", "max", "mean", "min", "nunique",
                    "size", "sum",
                }
                for node in ast.walk(expression_tree)
            ):
                # Counting or aggregating an identifier produces a metric. The
                # source field remains a dimension, but the rendered value does not.
                return False
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

    @staticmethod
    def _attach_report_markdown_table_origins(
        outputs: list[dict[str, Any]],
        *,
        code: str,
    ) -> list[dict[str, Any]]:
        """Recover structured column origins for Markdown tables in reports.

        `build_markdown_table(frame, columns=[...])` already carries a stable
        report-table schema.  Reusing it keeps Markdown table evidence at the
        column boundary and avoids asking the semantic planner to infer one
        metric for every rendered cell.
        """
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return outputs

        contracts: list[dict[str, Any]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            if function_name != "build_markdown_table" or not node.args:
                continue
            try:
                source_frame = ast.unparse(node.args[0]).strip()
            except Exception:
                source_frame = ""
            columns_node = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "columns"),
                None,
            )
            columns: dict[str, str] = {}
            if isinstance(columns_node, (ast.List, ast.Tuple)):
                for item in columns_node.elts:
                    if not isinstance(item, ast.Dict):
                        continue
                    values: dict[str, str] = {}
                    for key, value in zip(item.keys, item.values):
                        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                            continue
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            values[key.value] = value.value
                    label = str(values.get("label") or values.get("key") or "").strip()
                    field = str(values.get("key") or "").strip()
                    if label and field:
                        columns[label] = field
            if columns:
                contracts.append({"source_frame": source_frame, "columns": columns})

        if not contracts:
            return outputs
        for output in outputs:
            if not output.get("posthoc_report_candidate"):
                continue
            group_id = str(output.get("report_candidate_group_id") or "")
            match = re.fullmatch(r"report_table_(\d+)_column_\d+", group_id)
            if match is None:
                continue
            table_index = int(match.group(1))
            if table_index >= len(contracts):
                continue
            contract = contracts[table_index]
            label = str(output.get("label") or "").strip()
            field = str(contract["columns"].get(label) or "").strip()
            if not field:
                continue
            output.update({
                "field": field,
                "lineage_fields": [field],
                "source_frame": str(contract.get("source_frame") or ""),
                "markdown_table_column_origin": True,
            })
        return outputs

    @classmethod
    def _attach_output_lineage(
        cls,
        outputs: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        *,
        code: str = "",
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
        structured_producers = {
            key: list(value) for key, value in producers.items()
        }
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                for producer_key in _producer_keys(target)[1:]:
                    structured_producers.setdefault(producer_key, []).append(
                        operation_id
                    )

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

        artifact_constructor_operation_ids: set[str] = set()
        for operation_id, operation in operation_by_id.items():
            try:
                called_names = _called_operation_names(
                    ast.parse(str(operation.get("source") or ""))
                )
            except SyntaxError:
                called_names = set()
            if called_names.intersection({
                "build_chart_result",
                "build_multi_metric_chart_result",
                "build_chart_suite",
                "build_table_result",
                "save_analysis_result",
            }):
                artifact_constructor_operation_ids.add(operation_id)

        resolved: list[dict[str, Any]] = []
        for raw_output in outputs:
            output = dict(raw_output)
            if output.get("structured_origin"):
                resolved.append(output)
                continue
            output_producers = (
                structured_producers
                if output.get("artifact_type") in {"chart", "table"}
                else producers
            )
            field = str(output.get("field") or "").strip()
            # ``len(records)`` depends on writes to ``records[...]`` as well as
            # the initial container assignment. Expand that dependency only
            # for this explicit report expression. A global expansion would
            # merge independent mapping-member calculation boundaries.
            len_container_match = (
                re.fullmatch(r"len\(\s*([A-Za-z_]\w*)\s*\)", field)
                if output.get("artifact_type") == "report"
                else None
            )
            if len_container_match:
                container_name = len_container_match.group(1)
                report_producers = {
                    key: list(value) for key, value in producers.items()
                }
                for operation_id, operation in operation_by_id.items():
                    if any(
                        container_name in _producer_keys(target)[1:]
                        for target in operation.get("targets") or []
                    ):
                        report_producers.setdefault(container_name, []).append(
                            operation_id
                        )
                output_producers = report_producers
            declared_lineage_fields = [
                str(item or "").strip()
                for item in output.get("lineage_fields") or []
                if str(item or "").strip()
            ]
            # A rendered mapping member (for example ``alarm_sum['count']``)
            # carries both a business field and a container identity. Keeping
            # the full member expression prevents a same-named field written
            # into an unrelated record from stealing its calculation boundary.
            expression_field = field if "[" in field else ""
            lineage_fields = (
                [expression_field]
                if expression_field
                else list(dict.fromkeys(
                    str(item or "").strip()
                    for item in (declared_lineage_fields or [field])
                    if str(item or "").strip()
                ))
            )
            direct = [
                str(item) for item in output.get("direct_operation_node_ids") or []
                if str(item) in operation_by_id
            ]
            exact_matches = [
                operation_id
                for operation_id, operation in operation_by_id.items()
                if not (
                    output.get("artifact_type") in {"chart", "table"}
                    and (
                        operation.get("kind") == "output_expression"
                        or operation_id in artifact_constructor_operation_ids
                    )
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
                        output_producers,
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
            boundary_ids = [
                str(item)
                for item in output.get("boundary_operation_node_ids") or []
                if str(item) in operation_by_id
            ]
            shared_partition_count_boundary = False
            if len(boundary_ids) == 1:
                boundary = operation_by_id.get(boundary_ids[0]) or {}
                boundary_source = str(boundary.get("source") or "")
                try:
                    boundary_calls = _called_operation_names(ast.parse(boundary_source))
                except SyntaxError:
                    boundary_calls = set()
                shared_partition_count_boundary = (
                    boundary.get("semantic_kind") == "aggregation"
                    and bool(boundary_calls.intersection({
                        "value_counts", "count", "size", "nunique", "len",
                    }))
                )
            heterogeneous_series_values = (
                len(series_value_sources) > 1
                and not shared_partition_count_boundary
            )
            if shared_partition_count_boundary:
                # Earlier syntax passes see two scalar literals in the target
                # column before they recover their common value_counts/count
                # ancestor.  The recovered boundary is authoritative.
                output["ambiguous_calculation_boundary"] = False
                output["heterogeneous_structured_value"] = False
            output["ambiguous_calculation_boundary"] = bool(
                output.get("ambiguous_calculation_boundary")
                or heterogeneous_series_values
            )
            output["heterogeneous_structured_value"] = bool(
                output.get("heterogeneous_structured_value")
                or heterogeneous_series_values
            )
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
                    if not (
                        output.get("artifact_type") in {"chart", "table"}
                        and operation_id in artifact_constructor_operation_ids
                    )
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
                            output_producers,
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
                output_producers,
            )
            if not boundaries and query_projection_matches:
                direct = list(dict.fromkeys([*direct, *query_projection_matches]))
                boundaries = cls._nearest_calculation_boundaries(
                    direct,
                    operation_by_id,
                    output_producers,
                )
            if not boundaries:
                boundaries = cls._query_load_boundaries(
                    direct,
                    operation_by_id,
                    output_producers,
                )
            if len(boundaries) == 1:
                recovered_boundary = operation_by_id.get(boundaries[0]) or {}
                recovered_source = str(recovered_boundary.get("source") or "")
                try:
                    recovered_calls = _called_operation_names(
                        ast.parse(recovered_source)
                    )
                except SyntaxError:
                    recovered_calls = set()
                if (
                    recovered_boundary.get("semantic_kind") == "aggregation"
                    and recovered_calls.intersection({
                        "value_counts", "count", "size", "nunique", "len",
                    })
                ):
                    output["ambiguous_calculation_boundary"] = False
                    output["heterogeneous_structured_value"] = False
            can_compare_calculation_families = (
                len(boundaries) > 1
                and all(
                    (operation_by_id.get(item) or {}).get("kind") == "calculation"
                    for item in boundaries
                )
            )
            if len(series_value_sources) > 1 and can_compare_calculation_families:
                boundary_families = {
                    cls._operation_family_signature(operation_by_id.get(item) or {})
                    for item in boundaries
                }
                write_signatures = {
                    signature
                    for item in boundaries
                    if (
                        signature := cls._operation_write_signature(
                            operation_by_id.get(item) or {}
                        )
                    )
                }
                distinct_calculation_families = (
                    len(boundary_families) > 1
                    and len(write_signatures) != 1
                )
                output["ambiguous_calculation_boundary"] = (
                    distinct_calculation_families
                )
                output["heterogeneous_structured_value"] = (
                    distinct_calculation_families
                )
            if (
                output.get("artifact_type") in {"chart", "table"}
                and output.get("ambiguous_calculation_boundary")
                and len(boundaries) > 1
            ):
                boundary_families = {
                    cls._operation_family_signature(operation_by_id.get(item) or {})
                    for item in boundaries
                }
                write_signatures = {
                    signature
                    for item in boundaries
                    if (
                        signature := cls._operation_write_signature(
                            operation_by_id.get(item) or {}
                        )
                    )
                }
                if len(boundary_families) > 1 and len(write_signatures) != 1:
                    output["heterogeneous_structured_value"] = True

            selected = set(direct)
            pending = list(direct)
            while pending:
                operation_id = pending.pop()
                operation = operation_by_id.get(operation_id) or {}
                for loaded_name in operation.get("loads") or []:
                    for producer_id in output_producers.get(str(loaded_name), []):
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
                output_producers,
                runtime_query_sources,
            )
            runtime_expression_query_ids = list(dict.fromkeys(
                str(query_id)
                for query_id in output.get(
                    "runtime_expression_query_node_ids"
                ) or []
                if str(query_id)
            ))
            if runtime_expression_query_ids:
                # The rendered selector resolves the concrete iteration that
                # produced this expression; the wider static mapping closure
                # contains sibling iterations and must not be merged back in.
                query_ids = runtime_expression_query_ids
            else:
                query_ids = list(dict.fromkeys([
                    *(
                        str(query_id)
                        for query_id in output.get("runtime_row_query_node_ids") or []
                        if str(query_id)
                    ),
                    *query_ids,
                ]))
            if source_query_node_id:
                query_ids = list(dict.fromkeys([source_query_node_id, *query_ids]))
            output["direct_operation_node_ids"] = direct
            output["candidate_operation_node_ids"] = ordered_ids
            output["candidate_query_node_ids"] = query_ids
            output["boundary_operation_node_ids"] = boundaries
            resolved.append(output)

        # A dynamic report template can render one numeric expression against
        # several independently queried frames. Static name-flow can reach the
        # shared loop variable but cannot identify which runtime iteration
        # produced each rendered occurrence. Keep those occurrences separate
        # for semantic resolution instead of silently assigning every section
        # to the first query reached through the loop container.
        mapping_iteration_ranges: list[tuple[int, int]] = []
        try:
            code_tree = ast.parse(code or "")
        except SyntaxError:
            code_tree = None
        if code_tree is not None:
            for loop in (
                node for node in ast.walk(code_tree) if isinstance(node, ast.For)
            ):
                iterator = loop.iter
                if not (
                    isinstance(iterator, ast.Call)
                    and isinstance(iterator.func, ast.Attribute)
                    and iterator.func.attr == "items"
                ):
                    continue
                mapping_iteration_ranges.append((
                    int(getattr(loop, "lineno", 0) or 0),
                    int(getattr(loop, "end_lineno", 0) or 0),
                ))
        available_query_ids = {
            str(operation.get("query_node_id") or "")
            for operation in operations
            if str(operation.get("query_node_id") or "")
        }

        report_outputs_by_anchor: dict[str, list[dict[str, Any]]] = {}
        for output in resolved:
            if output.get("artifact_type") != "report":
                continue
            anchor_id = str(output.get("source_anchor_id") or "")
            if anchor_id:
                report_outputs_by_anchor.setdefault(anchor_id, []).append(output)
        for anchor_outputs in report_outputs_by_anchor.values():
            unresolved_occurrences = [
                output
                for output in anchor_outputs
                if not output.get("presentation_ordinal")
                and not output.get("candidate_query_node_ids")
                and not output.get("boundary_operation_node_ids")
            ]
            if len(unresolved_occurrences) > 1:
                for output in unresolved_occurrences:
                    output["ambiguous_calculation_boundary"] = True

            source_line = int(anchor_outputs[0].get("source_line") or 0)
            in_mapping_iteration = any(
                start <= source_line <= end
                for start, end in mapping_iteration_ranges
            )
            headings = {
                str((output.get("display_context") or {}).get("heading") or "").strip()
                for output in anchor_outputs
                if str((output.get("display_context") or {}).get("heading") or "").strip()
            }
            local_query_boundaries = {
                tuple(sorted(
                    str(query_id)
                    for query_id in output.get("candidate_query_node_ids") or []
                    if str(query_id)
                ))
                for output in anchor_outputs
            }
            if (
                len(anchor_outputs) > 1
                and in_mapping_iteration
                and len(headings) > 1
                and len(available_query_ids) > 1
                and len(local_query_boundaries) == 1
            ):
                for output in anchor_outputs:
                    output["ambiguous_calculation_boundary"] = True
                    output["runtime_iteration_boundary_required"] = True
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
        literal_fields = {
            str(item.value)
            for item in ast.walk(tree)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        named_output_fields = {
            str(item.arg)
            for item in ast.walk(tree)
            if isinstance(item, ast.keyword) and item.arg
        }
        return field in literal_fields or field in named_output_fields

    @staticmethod
    def _target_produces_field(target: str, field: str) -> bool:
        normalized_target = str(target or "").strip()
        if normalized_target == field:
            return True
        if "[" in field:
            field_root = field.split("[", 1)[0]
            if normalized_target == field_root:
                return True
            # A rendered expression such as ``alarm_sum['count']`` can be
            # produced by its container assignment, but not by an unrelated
            # record write such as ``rows['count']`` later in the program.
            # Field suffix matching is only sound within the same container.
            if "[" in normalized_target:
                return normalized_target.startswith(f"{field_root}[") and (
                    normalized_target.endswith(f"[{field.split('[', 1)[1]}")
                )
            return False
        return normalized_target.endswith(f"[{field!r}]") or normalized_target.endswith(f'["{field}"]')

    @staticmethod
    def _locate_report_outputs(report: str, expressions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        occupied: set[tuple[int, int]] = set()

        def display_context(start: int, end: int) -> dict[str, str]:
            line_start = report.rfind("\n", 0, start) + 1
            line_end = report.find("\n", end)
            if line_end < 0:
                line_end = len(report)
            heading = ""
            for candidate_line in reversed(report[:line_start].splitlines()):
                if candidate_line.strip().startswith("#"):
                    heading = candidate_line.strip().lstrip("#").strip()
                    break
            return {
                "heading": heading,
                "column_label": heading,
                "line": report[line_start:line_end].strip(),
                "token_prefix": report[max(line_start, start - 120):start],
                "token_suffix": report[end:min(line_end, end + 120)],
            }

        grouped: dict[str, list[dict[str, Any]]] = {}
        for expression in expressions:
            template_id = str(expression.get("template_id") or expression.get("expression_id") or "")
            grouped.setdefault(template_id, []).append(expression)
        for template_id, template_expressions in grouped.items():
            template_expressions.sort(key=lambda item: int(item.get("slot_index") or 0))
            tokens = template_expressions[0].get("template_tokens") or []
            if not tokens:
                continue
            region_start = 0
            for anchor in template_expressions[0].get("report_before_anchors") or []:
                position = report.find(str(anchor), region_start)
                if position >= 0:
                    region_start = position + len(str(anchor))
            region_end = len(report)
            for anchor in template_expressions[0].get("report_after_anchors") or []:
                position = report.find(str(anchor), region_start)
                if position >= 0:
                    region_end = position
                    break
            if region_end < region_start:
                continue
            report_region = report[region_start:region_end]
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
                        # The value may itself be a fully rendered report section.
                        # Keep the slot non-greedy, but allow newlines so the complete
                        # AST-owned template remains the source of positional truth.
                        pattern_parts.append(fr"(?P<{slot_name}>[\s\S]*?)")
            if not slot_names or not any("literal" in token and str(token.get("literal") or "") for token in tokens):
                continue
            pattern = "".join(pattern_parts)
            template_matches = list(re.finditer(pattern, report_region))
            for occurrence, match in enumerate(template_matches):
                rendered_slot_bindings = {
                    str(expression.get("expression") or ""): match.group(
                        f"slot_{int(expression.get('slot_index') or 0)}"
                    )
                    for expression in template_expressions
                    if f"slot_{int(expression.get('slot_index') or 0)}" in match.groupdict()
                }
                for expression in template_expressions:
                    slot_name = f"slot_{int(expression.get('slot_index') or 0)}"
                    if slot_name not in match.groupdict():
                        continue
                    local_start, local_end = match.span(slot_name)
                    start = region_start + local_start
                    end = region_start + local_end
                    text = report[start:end]
                    if start == end or (start, end) in occupied or not _is_numeric_display_text(text):
                        continue
                    occupied.add((start, end))
                    outputs.append({
                        "output_node_id": f"out_report_{expression['expression_id']}_{occurrence}",
                        "source_anchor_id": str(expression.get("expression_id") or ""),
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
                        "display_unit_hint": LineageIndexer._explicit_display_unit(
                            str(expression.get("numeric_suffix") or ""),
                            str(expression.get("suffix") or ""),
                        ),
                        "presentation_ordinal": bool(
                            expression.get("presentation_ordinal")
                        ),
                        "rendered_context": report[max(0, start - 120):min(len(report), end + 120)],
                        "display_context": display_context(start, end),
                        "source_line": expression.get("line"),
                        "numeric_hint": bool(expression.get("numeric_hint")),
                        "rendered_slot_bindings": rendered_slot_bindings,
                        "direct_operation_node_ids": [expression.get("operation_node_id")],
                    })

            # Large report f-strings frequently contain dynamic prose slots. A newline in
            # one prose value must not disconnect every numeric slot in the same template.
            # Fall back only to AST-declared numeric slots and their adjacent literals;
            # never scan arbitrary rendered Markdown numbers.
            if template_matches:
                continue
            for expression in template_expressions:
                if not expression.get("numeric_hint"):
                    continue
                prefix = str(expression.get("prefix") or "")
                suffix = str(expression.get("suffix") or "")
                local_prefix = prefix.rsplit("\n", 1)[-1][-80:]
                local_suffix = suffix.split("\n", 1)[0][:80]
                if not local_prefix and not local_suffix:
                    continue
                number_pattern = (
                    r"(?P<number>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)"
                    r"(?:\.\d+)?(?:[eE][-+]?\d+)?)"
                )
                local_pattern = (
                    re.escape(local_prefix)
                    + number_pattern
                    + (re.escape(str(expression.get("numeric_suffix") or "")))
                    + re.escape(local_suffix)
                )
                for occurrence, match in enumerate(re.finditer(local_pattern, report_region)):
                    local_start, local_end = match.span("number")
                    start = region_start + local_start
                    end = region_start + local_end
                    text = report[start:end]
                    if (start, end) in occupied or not _is_numeric_display_text(text):
                        continue
                    occupied.add((start, end))
                    outputs.append({
                        "output_node_id": f"out_report_{expression['expression_id']}_local_{occurrence}",
                        "source_anchor_id": str(expression.get("expression_id") or ""),
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
                        "display_prefix": prefix,
                        "display_suffix": suffix,
                        "display_unit_hint": LineageIndexer._explicit_display_unit(
                            str(expression.get("numeric_suffix") or ""),
                            suffix,
                        ),
                        "presentation_ordinal": bool(
                            expression.get("presentation_ordinal")
                        ),
                        "rendered_context": report[max(0, start - 120):min(len(report), end + 120)],
                        "display_context": display_context(start, end),
                        "source_line": expression.get("line"),
                        "numeric_hint": True,
                        "direct_operation_node_ids": [expression.get("operation_node_id")],
                    })
        return outputs

    @staticmethod
    def _locate_report_numeric_candidates(
        report: str,
        *,
        occupied: set[tuple[int, int]],
    ) -> list[dict[str, Any]]:
        """Enumerate numeric report spans that AST template matching did not locate.

        This is intentionally a recall-oriented artifact scan.  It does not
        declare that a token is a metric and it does not infer a calculation
        boundary.  Those decisions remain with the semantic planner and the
        local code/query reconciliation step.
        """
        token_pattern = re.compile(
            r"(?:(?<![A-Za-z0-9_.\-%])[-+]|(?<![A-Za-z0-9_.]))"
            r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?"
        )
        outputs: list[dict[str, Any]] = []
        in_fence = False
        line_start = 0
        heading = ""
        table_index = -1
        in_markdown_table = False
        table_headers: list[str] = []

        def presentation_literal_flags(
            line_text: str,
            *,
            token_start: int,
            token_end: int,
        ) -> tuple[bool, bool, bool, bool, bool, bool]:
            """Classify rendered display literals without inferring a metric.

            The artifact scan intentionally starts broad, but calendar values and
            comparison cutoffs are presentation grammar, not observed facts. This
            uses only local Markdown/natural-language syntax around the rendered
            token; it never relies on a business field name or a concrete value.
            """
            temporal_patterns = (
                r"\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?",
                r"\d{4}-\d{1,2}(?:-\d{1,2})?",
                r"\d{1,2}月(?:\d{1,2}日)?",
                (
                    r"(?<!\d)\d{1,2}[-/.]\d{1,2}[ T]"
                    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                    r"(?:Z|[+-]\d{2}:?\d{2})?(?!\d)"
                ),
                (
                    r"\d{4}-\d{1,2}-\d{1,2}[T\s]"
                    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                    r"(?:Z|[+-]\d{2}:?\d{2})?"
                ),
                (
                    r"(?<!\d)\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                    r"(?:Z|[+-]\d{2}:?\d{2})?(?!\d)"
                ),
            )
            temporal = any(
                match.start() <= token_start and token_end <= match.end()
                for pattern in temporal_patterns
                for match in re.finditer(pattern, line_text)
            )
            if not temporal:
                # A compact end date commonly omits the year after a complete
                # start date, for example ``2026-08-03 至 08-09``.  Classify
                # both date fragments as presentation literals while leaving
                # ordinary arithmetic fractions untouched.
                compact_date_ranges = (
                    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
                    r"\s*(?:至|到|~|～|—|–)\s*"
                    r"\d{1,2}[-/.]\d{1,2}",
                    r"(?:日期|周期|时间|截至|自)\s*[:：]?\s*"
                    r"\d{1,2}[-/.]\d{1,2}",
                )
                temporal = any(
                    match.start() <= token_start and token_end <= match.end()
                    for pattern in compact_date_ranges
                    for match in re.finditer(pattern, line_text)
                )
            if not temporal:
                relative_time_scope = (
                    r"(?:近|最近|过去|前)\s*\d+(?:\.\d+)?\s*"
                    r"(?:秒|分钟|分|小时|时|天|日|周|星期|月|季度|季|年)"
                )
                temporal = any(
                    match.start() <= token_start and token_end <= match.end()
                    for match in re.finditer(relative_time_scope, line_text)
                )
            if not temporal:
                window_grain = (
                    r"\d+(?:\.\d+)?\s*"
                    r"(?:秒|分钟|分|小时|时|天|日|周|星期|月|季度|季|年)"
                    r"\s*(?:窗口|粒度|间隔|步长)"
                )
                temporal = any(
                    match.start() <= token_start and token_end <= match.end()
                    for match in re.finditer(window_grain, line_text)
                )
            prefix = line_text[max(0, token_start - 24):token_start]
            suffix = line_text[token_end:min(len(line_text), token_end + 24)]
            comparison_threshold = bool(
                re.search(
                    r"(?:高于|低于|超过|不少于|不低于|至多|至少|大于|小于|>=|<=|>|<|≥|≤)\s*(?:\*\*)?\s*$",
                    prefix,
                )
            )
            if not comparison_threshold:
                # Query/filter disclosures such as ``status=0`` describe a
                # selection condition, not an observed report metric.  Limit
                # this grammar to machine-style identifiers so ordinary prose
                # statements remain eligible for Evidence discovery.
                comparison_threshold = bool(re.search(
                    r"[A-Za-z_][A-Za-z0-9_.]*\s*(?:={1,2}|!=)\s*$",
                    prefix,
                ))
            if not comparison_threshold:
                # A labelled interval such as ``20%~50%（medium）`` defines a
                # presentation bucket.  Its endpoints are cutoffs; the count
                # or measure reported after the label remains an observed fact.
                labelled_interval_pattern = (
                    r"(?<![\d.])\d+(?:\.\d+)?\s*%?\s*"
                    r"(?:~|～|至|到|—|–)\s*"
                    r"\d+(?:\.\d+)?\s*%?\s*"
                    r"[（(][^）)\n]{1,32}[）)]"
                )
                comparison_threshold = any(
                    match.start() <= token_start and token_end <= match.end()
                    for match in re.finditer(labelled_interval_pattern, line_text)
                )
            ordinal_patterns = (
                r"^\s*#{1,6}\s+(?:第\s*)?\d+(?:\.\d+)*[.、]?",
                r"^\s*\*\*(?:第\s*)?\d+(?:\.\d+)*[.、]?(?=\s|\*\*)",
                r"^\s*第\s*\d+(?:\.\d+)*[章节项]",
                r"^\s*(?:[-+*]\s+)?(?:\*\*)?\d+(?:\.\d+)*(?:[.)、]|）)(?=\s|\*\*)",
                # Ranked findings are often rendered on one line and separated
                # by semicolons (for example ``1. A; 2. B; 3. C``). Each rank
                # label is presentation grammar even when it is not at the
                # beginning of the Markdown line.
                r"(?:^|[;\uff1b]\s*)(?:\*\*)?\d+(?:\.\d+)*(?:[.)\u3001]|\uff09)(?=\s|\*\*)",
                # A report can refer back to a question/finding in normal
                # prose (not only as a Markdown heading). These numbers are
                # presentation labels, never observed values.
                r"(?:(?i:question)|\u95ee\u9898|\u53d1\u73b0)\s*\d+(?=\D|$)",
            )
            ordinal = any(
                match.start() <= token_start and token_end <= match.end()
                for pattern in ordinal_patterns
                for match in re.finditer(pattern, line_text)
            )
            formula_coefficient = bool(
                re.search(r"(?:[×*/xX]\s*|\bcoef(?:ficient)?\s*=\s*)$", prefix)
                or re.match(r"\s*[×*/xX+]", suffix)
                or (
                    re.search(r"(?:±|\+/-)\s*$", prefix)
                    and re.match(
                        r"\s*(?:σ|sigma\b|倍(?:标准差|方差)?)",
                        suffix,
                        flags=re.IGNORECASE,
                    )
                )
            )
            identifier_start = token_start
            identifier_end = token_end
            while identifier_start > 0 and re.match(
                r"[A-Za-z0-9_-]", line_text[identifier_start - 1]
            ):
                identifier_start -= 1
            while identifier_end < len(line_text) and re.match(
                r"[A-Za-z0-9_-]", line_text[identifier_end]
            ):
                identifier_end += 1
            identifier_token = line_text[identifier_start:identifier_end]
            business_identifier = bool(
                (identifier_start < token_start or identifier_end > token_end)
                and re.search(r"[A-Za-z]", identifier_token)
                and re.search(r"\d", identifier_token)
            )
            display_limit = bool(
                re.search(r"(?:\btop\s*|前\s*)$", prefix, flags=re.IGNORECASE)
                and re.match(
                    r"\s*(?:(?:个|名|项|条|行|组|位|家|款|批|类)|"
                    r"(?:products?|items?|rows?|groups?|categories?)\b)",
                    suffix,
                    flags=re.IGNORECASE,
                )
            )
            return (
                temporal,
                comparison_threshold,
                ordinal,
                formula_coefficient,
                business_identifier,
                display_limit,
            )

        for line_number, line in enumerate(report.splitlines(keepends=True), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                line_start += len(line)
                continue
            if not in_fence and stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
            if not in_fence:
                is_markdown_table_row = (
                    stripped.startswith("|") and stripped.count("|") >= 2
                )
                if is_markdown_table_row and not in_markdown_table:
                    table_index += 1
                    table_headers = []
                in_markdown_table = is_markdown_table_row
                if is_markdown_table_row and not table_headers:
                    table_headers = [
                        cell.strip()
                        for cell in stripped.strip("|").split("|")
                    ]
                for match in token_pattern.finditer(line):
                    start = line_start + match.start()
                    end = line_start + match.end()
                    if any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied):
                        continue
                    text = report[start:end]
                    if not _is_numeric_display_text(text):
                        continue
                    temporal_literal = bool(re.match(
                        r"^\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?",
                        line[match.start():],
                    ))
                    (
                        temporal_literal,
                        comparison_threshold,
                        presentation_ordinal,
                        presentation_formula_coefficient,
                        presentation_business_identifier,
                        presentation_display_limit,
                    ) = presentation_literal_flags(
                        line,
                        token_start=match.start(),
                        token_end=match.end(),
                    )
                    # Markdown headings label a section rather than report an
                    # observed metric.  Heading styles vary widely (for
                    # example, "Finding 1" and Chinese titles without a
                    # separator), so the Markdown grammar is the stable
                    # boundary instead of a vocabulary-specific pattern.
                    if stripped.startswith("#"):
                        presentation_ordinal = True
                    occupied.add((start, end))
                    pipe_before_match = line[:match.start()].count("|")
                    table_column_index = max(0, pipe_before_match - 1)
                    candidate_group_id = (
                        f"report_table_{table_index}_column_{table_column_index}"
                        if is_markdown_table_row
                        else f"report_span_{start}_{end}"
                    )
                    table_column_label = (
                        table_headers[table_column_index]
                        if table_column_index < len(table_headers)
                        else ""
                    )
                    outputs.append({
                        "output_node_id": f"out_report_candidate_{start}_{end}",
                        "artifact_type": "report",
                        "artifact_index": 0,
                        "field": "",
                        "label": table_column_label or heading,
                        "lineage_fields": [],
                        "start": start,
                        "end": end,
                        "text": text,
                        "numeric_hint": True,
                        "presentation_temporal_literal": temporal_literal,
                        "presentation_comparison_threshold": comparison_threshold,
                        "presentation_ordinal": presentation_ordinal,
                        "presentation_formula_coefficient": presentation_formula_coefficient,
                        "presentation_business_identifier": presentation_business_identifier,
                        "presentation_display_limit": presentation_display_limit,
                        "posthoc_report_candidate": True,
                        "report_candidate_group_id": candidate_group_id,
                        "report_line": line_number,
                        "rendered_context": report[max(0, start - 120):min(len(report), end + 120)],
                        "display_context": {
                            "heading": heading,
                            "column_label": table_column_label,
                            "line": stripped[:320],
                            "token_prefix": line[max(0, match.start() - 120):match.start()],
                            "token_suffix": line[match.end():min(len(line), match.end() + 120)],
                        },
                    })
            line_start += len(line)
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
        attribute_fields = list(dict.fromkeys(
            item.attr
            for item in ast.walk(tree)
            if isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and str(item.attr or "").strip()
        ))
        if attribute_fields:
            return attribute_fields
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
        execution = value.get("execution") if isinstance(value.get("execution"), dict) else {}
        return {
            "query_constraints": value.get("query_constraints") or {},
            "aggregation_hints": implementation.get("aggregation_hints") or [],
            "implementation": implementation,
            "execution_columns": execution.get("columns") or [],
        }


class PostAnalysisEvidenceCompiler:
    def __init__(
        self,
        *,
        planner: Callable[[dict[str, Any]], EvidencePlan] | None = None,
        structured_planner: Callable[[dict[str, Any]], EvidencePlan] | None = None,
    ) -> None:
        self._uses_default_planner = planner is None
        self._planner = planner or self._invoke_planner
        self._structured_planner = (
            structured_planner or self._invoke_structured_planner
        )
        self._resolver_id_maps: dict[str, dict[str, str]] = {}

    @staticmethod
    def _query_reachable_fields(
        query_id: str,
        query_snapshot: dict[str, Any] | None,
        operation_by_id: dict[str, dict[str, Any]],
        producers: dict[str, list[str]],
    ) -> set[str]:
        """Return direct and derived fields proven to depend on one query."""
        fields: set[str] = set()
        query_snapshot = query_snapshot if isinstance(query_snapshot, dict) else {}
        execution = (
            query_snapshot.get("execution")
            if isinstance(query_snapshot.get("execution"), dict)
            else {}
        )
        fields.update(
            str(field)
            for field in execution.get("columns") or []
            if str(field)
        )
        implementation = (
            query_snapshot.get("implementation")
            if isinstance(query_snapshot.get("implementation"), dict)
            else {}
        )
        for projection in implementation.get("projection_semantics") or []:
            if not isinstance(projection, dict):
                continue
            projection_fields = projection.get("source_fields") or []
            if not isinstance(projection_fields, list):
                projection_fields = [projection_fields]
            fields.update(
                str(field)
                for field in [projection.get("alias"), *projection_fields]
                if str(field)
            )
        for operation_id, operation in operation_by_id.items():
            if query_id not in LineageIndexer._query_dependencies(
                [operation_id],
                operation_by_id,
                producers,
            ):
                continue
            for target in operation.get("targets") or []:
                target_text = str(target).strip()
                if not target_text:
                    continue
                fields.add(target_text)
                subscript_field = re.search(
                    r"\[['\"](?P<field>[^'\"]+)['\"]\]$",
                    target_text,
                )
                if subscript_field:
                    fields.add(subscript_field.group("field"))
        return fields

    def compile(self, snapshot: AnalysisExecutionSnapshot) -> dict[str, Any]:
        lineage_index = LineageIndexer().build(snapshot)
        if not lineage_index["query_nodes"] or not (
            lineage_index["output_nodes"]
            or lineage_index.get("report_source_anchors")
        ):
            return self._empty_bundle(snapshot, lineage_index, "insufficient_lineage")
        calculation_candidates = self._build_calculation_candidates(lineage_index)
        report_semantic_candidates = self._build_report_semantic_candidates(
            lineage_index,
            calculation_candidates,
        )
        all_candidates = {**calculation_candidates, **report_semantic_candidates}
        has_structured_metric_outputs = any(
            output.get("artifact_type") in {"chart", "table"}
            and not self._is_grouping_dimension_output(output, lineage_index)
            for output in lineage_index.get("output_nodes") or []
        )
        if not all_candidates and not has_structured_metric_outputs:
            return self._empty_bundle(snapshot, lineage_index, "insufficient_calculation_boundaries")
        structured_plan = self._build_structured_evidence_plan(
            calculation_candidates,
            lineage_index,
        )
        structured_recovery_plan = EvidencePlan()
        structured_recovery_candidates: dict[str, dict[str, Any]] = {}
        unresolved_structured_outputs = self._unresolved_structured_outputs(
            lineage_index,
            structured_plan,
        )
        ambiguous_structured_outputs = [
            output for output in unresolved_structured_outputs
            if output.get("heterogeneous_structured_value")
        ]
        recoverable_structured_outputs = [
            output for output in unresolved_structured_outputs
            if not output.get("heterogeneous_structured_value")
        ]
        if ambiguous_structured_outputs:
            structured_recovery_plan = EvidencePlan(
                unresolved_outputs=[
                    UnresolvedEvidenceOutput(
                        output_node_id=str(output.get("output_node_id") or ""),
                        reason=(
                            "One structured value field contains multiple independent "
                            "calculation boundaries and cannot bind to one Evidence."
                        ),
                    )
                    for output in ambiguous_structured_outputs
                ]
            )
        if recoverable_structured_outputs:
            try:
                raw_structured_plan = self._structured_planner(
                    self._structured_resolver_payload(
                        snapshot,
                        lineage_index,
                        recoverable_structured_outputs,
                    )
                )
                (
                    recovered_structured_plan,
                    structured_recovery_candidates,
                ) = self._materialize_structured_recovery_plan(
                    snapshot,
                    lineage_index,
                    raw_structured_plan,
                    recoverable_structured_outputs,
                )
                structured_recovery_plan = EvidencePlan(
                    metrics=recovered_structured_plan.metrics,
                    unresolved_outputs=[
                        *structured_recovery_plan.unresolved_outputs,
                        *recovered_structured_plan.unresolved_outputs,
                    ],
                )
                calculation_candidates.update(structured_recovery_candidates)
                all_candidates.update(structured_recovery_candidates)
            except Exception as exc:
                logger.warning(
                    "post-analysis structured evidence resolver unavailable: execution_id=%s error=%s",
                    snapshot.execution_id,
                    exc,
                    exc_info=True,
                )
                structured_recovery_plan = EvidencePlan(
                    unresolved_outputs=[
                        UnresolvedEvidenceOutput(
                            output_node_id=str(output.get("output_node_id") or ""),
                            reason=f"Structured lineage resolver failed: {str(exc)[:300]}",
                        )
                        for output in recoverable_structured_outputs
                    ] + list(structured_recovery_plan.unresolved_outputs)
                )
        structured_candidates = self._filter_candidates_by_artifact_type(
            calculation_candidates,
            lineage_index,
            {"chart", "table"},
        )
        # A report-wide semantic scope is valid even when the local output
        # parser found no slots. The planner must still inspect the successful
        # code; local materialization may then isolate an unproven discovery.
        report_candidates = report_semantic_candidates
        verified_report_plan = self._build_verified_report_completion_plan(
            EvidencePlan(),
            calculation_candidates,
            lineage_index,
        )
        verified_report_output_ids = {
            str(output_id)
            for metric in verified_report_plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id)
        }
        planner_error = ""
        try:
            planner_payload = self._planner_fact_payload(
                snapshot,
                lineage_index,
                report_candidates,
                recovery_only=self._uses_default_planner,
                excluded_output_ids=verified_report_output_ids,
            ) if report_candidates else {}
            planner_required = bool(
                planner_payload.get("report_candidate_catalog")
                or planner_payload.get("source_anchor_catalog")
            ) or (bool(report_candidates) and not self._uses_default_planner)
            raw_report_plan = (
                self._restore_resolver_plan_ids(self._planner(planner_payload))
                if planner_required
                else EvidencePlan()
            )
            base_lineage_index = copy.deepcopy(lineage_index)
            base_report_candidates = copy.deepcopy(report_candidates)

            def finalize_report_plan(
                raw_plan: EvidencePlan,
                attempt_lineage: dict[str, Any],
                attempt_candidates: dict[str, dict[str, Any]],
            ) -> EvidencePlan:
                finalized = self._materialize_report_discovery_plan(
                    snapshot,
                    attempt_lineage,
                    raw_plan,
                    attempt_candidates,
                )
                finalized = self._sanitize_semantic_plan(finalized, attempt_candidates)
                finalized = self._isolate_duplicate_report_output_claims(
                    finalized,
                    attempt_candidates,
                )
                finalized = self._normalize_unresolved_report_outputs(finalized)
                return self._filter_plan_claims(
                    finalized,
                    excluded_output_ids=verified_report_output_ids,
                    allowed_output_ids={
                        str(output_id)
                        for candidate in attempt_candidates.values()
                        for output_id in candidate.get("output_node_ids") or []
                        if str(output_id)
                    },
                )

            first_lineage = copy.deepcopy(base_lineage_index)
            first_candidates = copy.deepcopy(base_report_candidates)
            report_plan = finalize_report_plan(
                raw_report_plan,
                first_lineage,
                first_candidates,
            )
            chosen_lineage = first_lineage
            chosen_candidates = first_candidates
            repairable_unresolved = [
                item
                for item in report_plan.unresolved_outputs
                if any(
                    token in str(item.reason or "")
                    for token in (
                        "unknown query fields",
                        "source locator",
                        "did not classify",
                    )
                )
            ]
            if self._uses_default_planner and planner_required and repairable_unresolved:
                repair_payload = copy.deepcopy(planner_payload)
                repair_payload["repair_feedback"] = {
                    "instruction": (
                        "Repair only the unresolved candidates below. Re-check the successful "
                        "query columns and the actual Python calculation before choosing query "
                        "IDs and metric fields. Do not change already resolved candidates."
                    ),
                    "unresolved": [
                        {
                            "output_node_id": item.output_node_id,
                            "reason": item.reason,
                        }
                        for item in repairable_unresolved
                    ],
                }
                repaired_raw_plan = self._restore_resolver_plan_ids(
                    self._planner(repair_payload)
                )
                repaired_lineage = copy.deepcopy(base_lineage_index)
                repaired_candidates = copy.deepcopy(base_report_candidates)
                repaired_plan = finalize_report_plan(
                    repaired_raw_plan,
                    repaired_lineage,
                    repaired_candidates,
                )
                if len(repaired_plan.unresolved_outputs) < len(report_plan.unresolved_outputs):
                    report_plan = repaired_plan
                    chosen_lineage = repaired_lineage
                    chosen_candidates = repaired_candidates
            lineage_index = chosen_lineage
            report_candidates = chosen_candidates
            all_candidates.update(report_candidates)
            report_reference_errors = self._plan_reference_errors(
                report_plan,
                all_candidates,
                require_complete=False,
            )
            if report_reference_errors:
                raise ValueError("; ".join(report_reference_errors[:20]))
            plan = EvidencePlan(
                metrics=[
                    *verified_report_plan.metrics,
                    *report_plan.metrics,
                    *structured_plan.metrics,
                    *structured_recovery_plan.metrics,
                ],
                unresolved_outputs=[
                    *report_plan.unresolved_outputs,
                    *structured_recovery_plan.unresolved_outputs,
                ],
                non_metric_report_candidate_ids=(
                    report_plan.non_metric_report_candidate_ids
                ),
            )
        except Exception as exc:
            logger.warning(
                "post-analysis evidence planner unavailable: execution_id=%s error=%s",
                snapshot.execution_id,
                exc,
                exc_info=True,
            )
            planner_error = str(exc)
            # Report semantics require the planner, but chart/table artifacts
            # already have deterministic column-level lineage. Isolate a report
            # planner failure instead of discarding those verified boundaries.
            plan = EvidencePlan(
                metrics=[
                    *verified_report_plan.metrics,
                    *structured_plan.metrics,
                    *structured_recovery_plan.metrics,
                ],
                unresolved_outputs=structured_recovery_plan.unresolved_outputs,
            )
            plan = self._isolate_invalid_metrics(plan, calculation_candidates)

        # Rendered report scanning is intentionally high recall.  Complete the
        # small set of positions that can be proven without another semantic
        # guess: repeated presentations of an already verified calculation and
        # explicit zero-result branches backed by a successful query snapshot.
        # This stays post-analysis and does not change the generated report.
        plan, posthoc_candidates = self._complete_proven_report_positions(
            snapshot,
            lineage_index,
            plan,
            all_candidates,
        )
        if posthoc_candidates:
            calculation_candidates.update(posthoc_candidates)
            all_candidates.update(posthoc_candidates)
        validation_candidates = (
            calculation_candidates if planner_error else all_candidates
        )
        reference_errors = self._plan_reference_errors(
            plan,
            validation_candidates,
            require_complete=False,
        )
        if reference_errors:
            # One invalid semantic binding must not erase independently proven
            # chart, table, or report boundaries. Reject invalid metrics one by
            # one, retain the verified subset, and surface their output slots as
            # unresolved. The verifier remains authoritative: no rejected metric
            # is materialized as Evidence.
            plan = self._isolate_invalid_metrics(plan, validation_candidates)
            remaining_reference_errors = self._plan_reference_errors(
                plan,
                validation_candidates,
                require_complete=False,
            )
            if remaining_reference_errors:
                return self._empty_bundle(
                    snapshot,
                    lineage_index,
                    "invalid_plan_references",
                    "; ".join(remaining_reference_errors[:20]),
                )
            if not planner_error:
                planner_error = "; ".join(reference_errors[:20])
        try:
            compiled = self._compile_plan(snapshot, lineage_index, plan, all_candidates)
            selected_semantic_output_ids = {
                str(output_id)
                for metric in plan.metrics
                for output_id in metric.output_node_ids
                if str(output_id)
            }
            candidate_output_ids = {
                str(output_id)
                for candidate in calculation_candidates.values()
                for output_id in candidate.get("output_node_ids") or []
                if str(output_id)
            }.union(selected_semantic_output_ids)
            metric_output_ids = {
                str(output.get("output_node_id") or "")
                for output in lineage_index.get("output_nodes") or []
                if str(output.get("output_node_id") or "")
                and not self._is_grouping_dimension_output(output, lineage_index)
                and self._is_report_metric_output(output, lineage_index)
            }
            structured_output_ids = {
                str(output.get("output_node_id") or "")
                for output in lineage_index.get("output_nodes") or []
                if str(output.get("output_node_id") or "")
                and output.get("artifact_type") in {"chart", "table"}
                and not self._is_grouping_dimension_output(output, lineage_index)
            }
            compiled_output_ids = set(compiled.get("diagnostics", {}).pop("compiled_output_node_ids", []))
            non_metric_candidate_ids = {
                str(output_id)
                for output_id in plan.non_metric_report_candidate_ids
                if str(output_id)
            }
            unresolved_output_ids = sorted(
                metric_output_ids - candidate_output_ids - non_metric_candidate_ids
            )
            plan_unresolved_output_ids = {
                str(item.output_node_id or "")
                for item in plan.unresolved_outputs
                if str(item.output_node_id or "")
            }
            missing_compiled_output_ids = sorted(
                candidate_output_ids - compiled_output_ids - plan_unresolved_output_ids
            )
            unresolved_structured_output_ids = sorted(
                structured_output_ids - candidate_output_ids
            )
            missing_structured_output_ids = sorted(
                structured_output_ids - compiled_output_ids
            )
            compiled["diagnostics"].update({
                "metric_output_count": len(metric_output_ids),
                "structured_output_slot_count": len(structured_output_ids),
                "structured_output_bound_count": len(
                    structured_output_ids.intersection(compiled_output_ids)
                ),
                "candidate_output_count": len(candidate_output_ids),
                "compiled_output_count": len(compiled_output_ids),
                "unresolved_output_node_ids": unresolved_output_ids,
                "missing_compiled_output_node_ids": missing_compiled_output_ids,
                "unresolved_structured_output_node_ids": unresolved_structured_output_ids,
                "missing_structured_output_node_ids": missing_structured_output_ids,
                "report_semantic_scope_count": len(report_semantic_candidates),
            })
            if planner_error:
                compiled["diagnostics"].update({
                    "reason": "planner_unavailable",
                    "planner_error": planner_error[:1200],
                })
            compiled["status"] = (
                "complete"
                if compiled["evidence_definitions"]
                and not unresolved_output_ids
                and not missing_compiled_output_ids
                and not plan_unresolved_output_ids
                and not missing_structured_output_ids
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

    def find_heterogeneous_structured_outputs(
        self,
        snapshot: AnalysisExecutionSnapshot,
    ) -> list[dict[str, Any]]:
        """Find chart/table value columns without one calculation boundary.

        This reuses the deterministic half of Evidence compilation. It does not
        invoke either planner and does not create Evidence; it only reports an
        artifact shape that the column-level trace contract cannot represent.
        """
        lineage_index = LineageIndexer().build(snapshot)
        if not lineage_index.get("query_nodes"):
            return []
        calculation_candidates = self._build_calculation_candidates(lineage_index)
        structured_plan = self._build_structured_evidence_plan(
            calculation_candidates,
            lineage_index,
        )
        return [
            output
            for output in self._unresolved_structured_outputs(
                lineage_index,
                structured_plan,
            )
            if output.get("heterogeneous_structured_value")
        ]

    def find_heterogeneous_rankings(
        self,
        snapshot: AnalysisExecutionSnapshot,
    ) -> list[dict[str, Any]]:
        """Find ordered containers whose members lack one comparison boundary."""
        lineage_index = LineageIndexer().build(snapshot)
        return [
            dict(item)
            for item in lineage_index.get("heterogeneous_rankings") or []
            if isinstance(item, dict)
        ]

    @staticmethod
    def _normalized_source_expression(expression: str) -> str:
        source = str(expression or "").strip()
        if not source:
            return ""
        try:
            return ast.dump(ast.parse(source, mode="eval").body, include_attributes=False)
        except SyntaxError:
            return re.sub(r"\s+", "", source)

    @staticmethod
    def _resolve_calculation_source_refs(
        code: str,
        lineage_index: dict[str, Any],
        references: list[CalculationSourceRef],
    ) -> tuple[list[str], list[str]]:
        """Resolve planner source locators into locally verifiable operations.

        A locator may point at syntax the regular operation index did not choose
        as a calculation boundary, such as an inline expression in a report
        f-string. The source location must identify exactly one AST node. The
        resulting synthetic operation still has to reach a successful query via
        the normal dependency graph before it can be materialized.
        """
        if not references:
            return [], []
        try:
            tree = ast.parse(code or "")
        except SyntaxError as exc:
            return [], [f"analysis code cannot be parsed: {exc}"]

        operations = lineage_index.setdefault("operation_nodes", [])
        operation_by_id = {
            str(item.get("operation_node_id") or ""): item
            for item in operations
            if str(item.get("operation_node_id") or "")
        }

        def source_identity(value: str) -> str:
            return re.sub(r"\s+", "", str(value or "").strip())

        def statement_identity(value: str) -> str:
            try:
                parsed = ast.parse(str(value or "").strip(), mode="exec")
            except SyntaxError:
                return ""
            if len(parsed.body) != 1:
                return ""
            return ast.dump(parsed.body[0], include_attributes=False)

        resolved: list[str] = []
        errors: list[str] = []
        for reference in references:
            line = int(reference.source_line or 0)
            expression = str(reference.expression or "").strip()
            if line <= 0 or not expression:
                errors.append("calculation source locator requires source_line and expression")
                continue
            wanted = source_identity(expression)
            wanted_statement = statement_identity(expression)
            mapping_value_identity = ""
            if ":" in expression:
                try:
                    mapping_value = expression.split(":", 1)[1].strip()
                    mapping_value_identity = ast.dump(
                        ast.parse(mapping_value, mode="eval").body,
                        include_attributes=False,
                    )
                except SyntaxError:
                    mapping_value_identity = ""
            exact_matches: list[ast.AST] = []
            assignment_matches: list[ast.AST] = []
            assignment_target_match = re.match(
                r"^([A-Za-z_]\w*)\s*=",
                expression,
            )
            assignment_target = (
                assignment_target_match.group(1)
                if assignment_target_match is not None
                else ""
            )
            for node in ast.walk(tree):
                node_start = int(getattr(node, "lineno", 0) or 0)
                node_end = int(getattr(node, "end_lineno", node_start) or node_start)
                if not (node_start - 2 <= line <= node_end + 2):
                    continue
                if not isinstance(node, (
                    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr,
                    ast.Call, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Subscript,
                    ast.Attribute, ast.Name, ast.List, ast.Tuple, ast.Dict,
                    ast.IfExp, ast.JoinedStr, ast.FormattedValue,
                )):
                    continue
                if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                    continue
                source = ast.get_source_segment(code, node) or ""
                node_identity = ast.dump(node, include_attributes=False)
                if (
                    source_identity(source) == wanted
                    or (
                        mapping_value_identity
                        and node_identity == mapping_value_identity
                    )
                    or (
                        wanted_statement
                        and statement_identity(source) == wanted_statement
                    )
                ):
                    exact_matches.append(node)
                    continue
                if (
                    assignment_target
                    and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    and assignment_target in _target_names(node)
                ):
                    assignment_matches.append(node)
            # A planner normally returns the exact source expression.  Target
            # matching is only a fallback for normalized assignment snippets;
            # mixing both tiers makes a nearby reassignment incorrectly turn an
            # otherwise exact locator into an ambiguous one.
            matches = exact_matches or assignment_matches
            if len(matches) > 1:
                exact_line_matches = [
                    node
                    for node in matches
                    if int(getattr(node, "lineno", 0) or 0) == line
                ]
                if exact_line_matches:
                    matches = exact_line_matches
            unique_locations = {
                (
                    int(getattr(node, "lineno", 0) or 0),
                    int(getattr(node, "col_offset", 0) or 0),
                    int(getattr(node, "end_lineno", 0) or 0),
                    int(getattr(node, "end_col_offset", 0) or 0),
                ): node
                for node in matches
            }
            if len(unique_locations) != 1:
                errors.append(
                    f"calculation source locator is not unique: line={line} expression={expression[:160]}"
                )
                continue
            node = next(iter(unique_locations.values()))
            source = ast.get_source_segment(code, node) or expression
            existing = next((
                operation_id
                for operation_id, operation in operation_by_id.items()
                if int(operation.get("line") or 0) == int(getattr(node, "lineno", line) or line)
                and source_identity(str(operation.get("source") or "")) == source_identity(source)
            ), "")
            if existing:
                resolved.append(existing)
                continue

            resolved_line = int(getattr(node, "lineno", line) or line)
            operation_id = f"op_recovered_{resolved_line}_{_digest(source, 10)}"
            operation = {
                "operation_node_id": operation_id,
                "line": resolved_line,
                "kind": "calculation",
                "semantic_kind": (
                    LineageIndexer._operation_semantic_kind(node)
                    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    else "formula"
                ),
                "query_node_id": "",
                "targets": _target_names(node),
                "loads": _loaded_names(node),
                "source": source[:1200],
                "planner_recovered": True,
            }
            if not operation["loads"]:
                errors.append(
                    f"calculation source locator has no data dependency: line={line} expression={expression[:160]}"
                )
                continue
            operations.append(operation)
            operation_by_id[operation_id] = operation
            resolved.append(operation_id)
        return list(dict.fromkeys(resolved)), errors

    def _materialize_report_discovery_plan(
        self,
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        plan: EvidencePlan,
        report_candidates: dict[str, dict[str, Any]],
    ) -> EvidencePlan:
        """Resolve planner anchors and rendered candidates into report output nodes.

        The planner discovers semantics from code.  This method is deliberately
        authoritative only about syntax and executed dataflow: it maps source
        anchors to rendered report slots, verifies selected operations, and
        derives query references from successful QuerySnapshot dependencies.
        """
        candidate = report_candidates.get("report_semantic_scope")
        if not candidate:
            return plan
        source_catalog = {
            str(item.get("anchor_id") or ""): item
            for item in lineage_index.get("report_source_anchors") or []
            if str(item.get("anchor_id") or "")
        }
        anchors_by_location: dict[tuple[int, str], dict[str, Any]] = {}
        for anchor in source_catalog.values():
            anchors_by_location[(
                int(anchor.get("source_line") or 0),
                self._normalized_source_expression(str(anchor.get("expression") or "")),
            )] = anchor

        output_by_id = {
            str(item.get("output_node_id") or ""): item
            for item in lineage_index.get("output_nodes") or []
            if str(item.get("output_node_id") or "")
        }
        outputs_by_anchor: dict[str, list[str]] = {}
        report_candidate_members: dict[str, list[str]] = {}
        for output_id, output in output_by_id.items():
            if output.get("artifact_type") != "report":
                continue
            if (
                output.get("posthoc_report_candidate")
                or output.get("ambiguous_calculation_boundary")
            ):
                candidate_id = str(
                    output.get("report_candidate_group_id")
                    if output.get("posthoc_report_candidate")
                    else output_id
                )
                report_candidate_members.setdefault(candidate_id, []).append(output_id)
            anchor_id = str(output.get("source_anchor_id") or "")
            if anchor_id:
                outputs_by_anchor.setdefault(anchor_id, []).append(output_id)

        operation_by_id = {
            str(item.get("operation_node_id") or ""): item
            for item in lineage_index.get("operation_nodes") or []
            if str(item.get("operation_node_id") or "")
        }
        producers: dict[str, list[str]] = {}
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                target_name = str(target or "").strip()
                if target_name:
                    producers.setdefault(target_name, []).append(operation_id)
        successful_query_ids = {
            str(item.get("query_node_id") or "")
            for item in lineage_index.get("query_nodes") or []
            if str(item.get("query_node_id") or "")
        }

        def selector_boundary_proves_output(
            output_id: str,
            metric: PlannedMetric,
            selected_operations: list[str],
        ) -> bool:
            selector = str(
                (metric.parameter_bindings or {}).get("mapping_selector") or ""
            )
            if not selector:
                return False
            output = output_by_id.get(output_id) or {}
            rendered_bindings = (
                output.get("rendered_slot_bindings")
                or output.get("slot_bindings")
                or {}
            )
            if selector not in {str(value) for value in rendered_bindings.values()}:
                return False
            return any(
                str((operation_by_id.get(operation_id) or {}).get(
                    "parameter_bindings", {}
                ).get("mapping_selector") or "") == selector
                for operation_id in selected_operations
            )

        def resolve_anchor(reference: ReportSourceAnchorRef) -> dict[str, Any] | None:
            anchor_id = str(reference.anchor_id or "")
            if anchor_id and anchor_id in source_catalog:
                return source_catalog[anchor_id]
            key = (
                int(reference.source_line or 0),
                self._normalized_source_expression(reference.expression),
            )
            return anchors_by_location.get(key)

        resolved_metrics: list[PlannedMetric] = []
        unresolved: list[UnresolvedEvidenceOutput] = []
        non_metric_anchor_ids: set[str] = {
            str(anchor.anchor_id or "")
            for anchor in plan.non_metric_source_anchors
            if str(anchor.anchor_id or "")
        }
        non_metric_candidate_ids = {
            output_id
            for item in plan.non_metric_report_candidate_ids
            for output_id in report_candidate_members.get(str(item), [])
        }
        report_candidate_labels = {
            candidate_id: str(
                (output_by_id.get(output_ids[0]) or {}).get("label") or ""
            )
            for candidate_id, output_ids in report_candidate_members.items()
            if output_ids
        }

        def normalized_label(value: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").lower())

        candidate_owners: dict[str, list[int]] = {}
        for metric_index, metric in enumerate(plan.metrics):
            for candidate_id in metric.report_candidate_ids:
                if str(candidate_id) in report_candidate_members:
                    candidate_owners.setdefault(str(candidate_id), []).append(metric_index)
        allowed_candidate_owners: dict[str, int | None] = {}
        unresolved_duplicate_candidate_outputs: list[UnresolvedEvidenceOutput] = []
        for candidate_id, owners in candidate_owners.items():
            if len(owners) == 1:
                allowed_candidate_owners[candidate_id] = owners[0]
                continue
            label = normalized_label(report_candidate_labels.get(candidate_id, ""))
            scores = {
                owner: int(bool(label) and (
                    label in normalized_label(plan.metrics[owner].business_name)
                    or normalized_label(plan.metrics[owner].business_name) in label
                    or label in normalized_label(plan.metrics[owner].metric_key)
                    or normalized_label(plan.metrics[owner].metric_key) in label
                ))
                for owner in owners
            }
            best_score = max(scores.values())
            winners = [owner for owner, score in scores.items() if score == best_score]
            if best_score and len(winners) == 1:
                allowed_candidate_owners[candidate_id] = winners[0]
                continue
            allowed_candidate_owners[candidate_id] = None
            unresolved_duplicate_candidate_outputs.extend(
                UnresolvedEvidenceOutput(
                    output_node_id=output_id,
                    reason="Multiple semantic claims could not be reduced to one report candidate boundary.",
                )
                for output_id in report_candidate_members[candidate_id]
            )
        for item in plan.unresolved_outputs:
            if item.source_anchor is None:
                unresolved.append(item)
                continue
            anchor = resolve_anchor(item.source_anchor)
            if anchor is None:
                unresolved.append(item)
                continue
            anchor_id = str(anchor.get("anchor_id") or "")
            output_ids = outputs_by_anchor.get(anchor_id) or []
            if not output_ids:
                unresolved.append(item)
                continue
            unresolved.extend(
                UnresolvedEvidenceOutput(
                    output_node_id=output_id,
                    source_anchor=item.source_anchor,
                    reason=item.reason,
                )
                for output_id in output_ids
            )

        for metric_index, metric in enumerate(plan.metrics):
            resolved_anchors = [
                anchor
                for reference in metric.source_anchors
                if (anchor := resolve_anchor(reference)) is not None
            ]
            anchor_output_ids = [
                output_id
                for anchor in resolved_anchors
                for output_id in outputs_by_anchor.get(str(anchor.get("anchor_id") or ""), [])
                if (
                    not metric.parameter_bindings
                    or (
                        (candidate.get("local_lineage_hints") or {})
                        .get(output_id, {})
                        .get("parameter_bindings", {})
                        == metric.parameter_bindings
                    )
                )
            ]
            selected_candidate_output_ids = [
                output_id
                for candidate_id in metric.report_candidate_ids
                if allowed_candidate_owners.get(str(candidate_id)) == metric_index
                for output_id in report_candidate_members.get(str(candidate_id), [])
            ]
            output_ids = list(dict.fromkeys([
                *anchor_output_ids,
                *selected_candidate_output_ids,
            ]))
            # A report source anchor is produced from the rendered expression
            # itself.  When that expression already has a locally resolved
            # operation boundary, it is a stronger calculation source than a
            # planner's compact source-line hint.  Keep the anchor operations
            # in the same dependency closure so harmless locator ambiguity
            # cannot discard an otherwise proven report metric.
            anchor_boundary_operations = list(dict.fromkeys(
                str(operation_id)
                for output_id in anchor_output_ids
                for operation_id in [
                    *((output_by_id.get(output_id) or {}).get(
                        "boundary_operation_node_ids"
                    ) or []),
                    *((output_by_id.get(output_id) or {}).get(
                        "direct_operation_node_ids"
                    ) or []),
                ]
                if str(operation_id) in operation_by_id
            ))
            recovered_operations, source_ref_errors = self._resolve_calculation_source_refs(
                snapshot.generated_code,
                lineage_index,
                metric.calculation_source_refs,
            )
            if recovered_operations:
                operation_by_id.update({
                    str(item.get("operation_node_id") or ""): item
                    for item in lineage_index.get("operation_nodes") or []
                    if str(item.get("operation_node_id") or "")
                })
                producers.clear()
                for operation_id, operation in operation_by_id.items():
                    for target in operation.get("targets") or []:
                        target_name = str(target or "").strip()
                        if target_name:
                            producers.setdefault(target_name, []).append(operation_id)
            selected_operations = list(dict.fromkeys(
                str(operation_id)
                for operation_id in [
                    *metric.operation_node_ids,
                    *anchor_boundary_operations,
                    *recovered_operations,
                ]
                if str(operation_id) in operation_by_id
            ))
            query_ids = list(dict.fromkeys(
                query_id
                for operation_id in selected_operations
                for query_id in LineageIndexer._query_dependencies(
                    [operation_id], operation_by_id, producers
                )
                if query_id in successful_query_ids
            ))
            verification = "verified"
            claimed_query_ids = list(dict.fromkeys(
                str(query_id)
                for query_id in metric.claimed_query_node_ids
                if str(query_id)
            ))
            output_verified_query_ids = {
                str(query_id)
                for output_id in output_ids
                for query_id in (output_by_id.get(output_id) or {}).get(
                    "candidate_query_node_ids"
                ) or []
                if str(query_id)
            }
            # For a posthoc report candidate, a source expression that the
            # local resolver has already mapped to real operations is stronger
            # evidence than the planner's compact query summary.  The latter
            # is needed only when no local operation/query closure exists.
            # Otherwise a correctly located calculation could be rejected just
            # because the planner omitted one upstream query from its summary.
            claimed_fields_for_boundary = {
                str(field).strip()
                for field in metric.claimed_metric_fields
                if str(field).strip()
            }
            locally_reachable_fields = {
                field
                for query_id in query_ids
                for field in self._query_reachable_fields(
                    query_id,
                    snapshot.query_snapshots.get(query_id),
                    operation_by_id,
                    producers,
                )
            }
            runtime_iteration_boundary = any(
                bool((output_by_id.get(output_id) or {}).get(
                    "runtime_iteration_boundary_required"
                ))
                for output_id in output_ids
            )
            runtime_identity_query_ids = {
                str(query_id)
                for output_id in output_ids
                if (output_by_id.get(output_id) or {}).get(
                    "runtime_artifact_query_identity"
                )
                for query_id in (output_by_id.get(output_id) or {}).get(
                    "candidate_query_node_ids"
                ) or []
                if str(query_id)
            }
            # A nested report string can hide the runtime loop occurrence from
            # static name-flow.  When the rendered heading uniquely proves a
            # physical query and that identity conflicts with the static
            # closure, the closure is capability-limited rather than a valid
            # contradiction.  Keep query/field attestation mandatory instead
            # of accepting either side without verification.
            runtime_identity_conflict = bool(
                runtime_identity_query_ids
                and query_ids
                and not runtime_identity_query_ids.issubset(set(query_ids))
            )
            ambiguous_output_boundary = bool(
                runtime_iteration_boundary or runtime_identity_conflict
            )
            output_local_boundary_proven = (
                bool(output_ids)
                and not ambiguous_output_boundary
                and all(
                    {
                        str(query_id)
                        for query_id in (output_by_id.get(output_id) or {}).get(
                            "candidate_query_node_ids"
                        ) or []
                        if str(query_id)
                    }.intersection(query_ids)
                    for output_id in output_ids
                )
            )
            explicit_operation_boundary_proven = bool(
                metric.operation_node_ids and query_ids
            )
            locally_resolved_query_boundary = bool(
                (
                    resolved_anchors
                    and anchor_boundary_operations
                    or metric.report_candidate_ids
                )
                and selected_operations
                and query_ids
                and not ambiguous_output_boundary
                and (
                    output_local_boundary_proven
                    or explicit_operation_boundary_proven
                )
                and (
                    not claimed_fields_for_boundary
                    or claimed_fields_for_boundary.issubset(
                        locally_reachable_fields
                    )
                )
            )
            if resolved_anchors and anchor_boundary_operations and query_ids:
                # `calculation_source_refs` are an LLM aid for source forms
                # the static parser cannot recover.  Do not let an ambiguous
                # hint override a report expression whose local dataflow has
                # already been proven through its anchor boundary.
                source_ref_errors = []
            if (
                output_verified_query_ids
                and claimed_query_ids
                and not output_verified_query_ids.issubset(set(claimed_query_ids))
                and not locally_resolved_query_boundary
                and not ambiguous_output_boundary
            ):
                source_ref_errors.append(
                    "LLM query claim contradicts locally verified query dependencies."
                )
            elif (
                query_ids
                and claimed_query_ids
                and not set(query_ids).issubset(set(claimed_query_ids))
                and not locally_resolved_query_boundary
                and not ambiguous_output_boundary
            ):
                source_ref_errors.append(
                    "Planner source locator does not support the declared query boundary."
                )
            requires_attestation = bool(
                (metric.report_candidate_ids or resolved_anchors)
                and not locally_resolved_query_boundary
                and claimed_query_ids
                and (
                    metric.claimed_metric_fields
                    or not query_ids
                    or source_ref_errors
                    or set(query_ids) != set(claimed_query_ids)
                )
            )
            if requires_attestation:
                unknown_queries = set(claimed_query_ids) - successful_query_ids
                query_fields_by_id = {
                    query_id: self._query_reachable_fields(
                        query_id,
                        snapshot.query_snapshots.get(query_id),
                        operation_by_id,
                        producers,
                    )
                    for query_id in successful_query_ids
                }
                claimed_fields = list(dict.fromkeys(
                    str(field).strip()
                    for field in metric.claimed_metric_fields
                    if str(field).strip()
                ))
                known_fields = {
                    field
                    for query_id in claimed_query_ids
                    for field in query_fields_by_id.get(query_id, set())
                }
                unknown_fields = set(claimed_fields) - known_fields
                if unknown_fields and claimed_fields:
                    compatible_queries = [
                        query_id
                        for query_id, fields in query_fields_by_id.items()
                        if set(claimed_fields).issubset(fields)
                    ]
                    if len(compatible_queries) == 1:
                        claimed_query_ids = compatible_queries
                        unknown_queries = set()
                        unknown_fields = set()
                if metric.attestation_confidence != "high":
                    source_ref_errors.append(
                        "LLM attestation requires high confidence when local dataflow is unavailable."
                    )
                elif not claimed_query_ids or unknown_queries:
                    source_ref_errors.append(
                        "LLM attestation references no successful query or an unknown query."
                    )
                elif unknown_fields:
                    source_ref_errors.append(
                        f"LLM attestation references unknown query fields: {sorted(unknown_fields)}"
                    )
                else:
                    source_identity = _stable_json([
                        item.model_dump() for item in metric.calculation_source_refs
                    ] or [{"report_candidate_ids": metric.report_candidate_ids}])
                    attested_operations: list[str] = []
                    for query_id in claimed_query_ids:
                        operation_id = (
                            f"op_attested_{_digest(source_identity + ':' + query_id, 14)}"
                        )
                        operation = {
                            "operation_node_id": operation_id,
                            "line": min(
                                (
                                    int(item.source_line or 0)
                                    for item in metric.calculation_source_refs
                                ),
                                default=0,
                            ),
                            "kind": "calculation",
                            "semantic_kind": "formula",
                            "query_node_id": query_id,
                            "targets": claimed_fields,
                            "loads": [],
                            "source": source_identity[:1200],
                            "planner_attested": True,
                        }
                        lineage_index.setdefault("operation_nodes", []).append(operation)
                        operation_by_id[operation_id] = operation
                        attested_operations.append(operation_id)
                    # Attestation proves the query/field boundary when static
                    # dataflow cannot. Keep any exact local calculation
                    # operations the planner already located so formula and
                    # transformation details remain available to Evidence.
                    selected_operations = list(dict.fromkeys([
                        *selected_operations,
                        *attested_operations,
                    ]))
                    query_ids = claimed_query_ids
                    verification = "attested"
                    # Source locators help local verification when supported, but
                    # a locator capability miss must not veto an otherwise
                    # consistent structured attestation. Query/field conflicts
                    # above remain authoritative contradictions.
                    source_ref_errors = [
                        error
                        for error in source_ref_errors
                        if "contradicts locally verified" in error
                    ]
            if verification == "verified" and query_ids:
                compatible_output_ids: list[str] = []
                selected_query_set = set(query_ids)
                for output_id in output_ids:
                    output = output_by_id.get(output_id) or {}
                    output_query_ids = {
                        str(query_id)
                        for query_id in output.get("candidate_query_node_ids") or []
                        if str(query_id)
                    }
                    output_operation_ids = {
                        str(operation_id)
                        for operation_id in output.get("candidate_operation_node_ids") or []
                        if str(operation_id)
                        and (operation_by_id.get(str(operation_id)) or {}).get("kind")
                        != "output_expression"
                    }
                    if (
                        (output_query_ids and output_query_ids & selected_query_set)
                        or selector_boundary_proves_output(
                            output_id,
                            metric,
                            selected_operations,
                        )
                        or (
                            output_id in selected_candidate_output_ids
                            and
                            not output_operation_ids
                            and not output_query_ids
                        )
                    ):
                        compatible_output_ids.append(output_id)
                        continue
                    unresolved.append(UnresolvedEvidenceOutput(
                        output_node_id=output_id,
                        reason=(
                            "Report output is locally proven outside the selected calculation "
                            "and query boundary."
                        ),
                    ))
                output_ids = compatible_output_ids
            if source_ref_errors or not output_ids or not selected_operations or not query_ids:
                reason = (
                    "; ".join(source_ref_errors)
                    if source_ref_errors
                    else "Planner source anchor cannot be proven to reach a report output and successful query."
                )
                failed_output_ids: set[str] = set()
                for anchor in resolved_anchors:
                    anchor_id = str(anchor.get("anchor_id") or "")
                    anchor_output_ids = outputs_by_anchor.get(anchor_id, [])
                    if not anchor_output_ids:
                        unresolved.append(UnresolvedEvidenceOutput(
                            source_anchor=ReportSourceAnchorRef(
                                anchor_id=anchor_id,
                                source_line=int(anchor.get("source_line") or 0),
                                expression=str(anchor.get("expression") or ""),
                            ),
                            reason=reason,
                        ))
                    for output_id in anchor_output_ids:
                        failed_output_ids.add(output_id)
                        unresolved.append(UnresolvedEvidenceOutput(
                            output_node_id=output_id,
                            reason=reason,
                        ))
                for output_id in selected_candidate_output_ids:
                    if output_id in failed_output_ids:
                        continue
                    unresolved.append(UnresolvedEvidenceOutput(
                        output_node_id=output_id,
                        reason=reason,
                    ))
                continue
            for operation_id in selected_operations:
                candidate.setdefault("operation_node_ids", []).append(operation_id)
                candidate.setdefault("boundary_operation_node_ids", []).append(operation_id)
                candidate.setdefault("operation_query_ids", {})[operation_id] = list(query_ids)
                candidate.setdefault("operation_semantic_kinds", {})[operation_id] = str(
                    (operation_by_id.get(operation_id) or {}).get("semantic_kind") or ""
                )
            for output_id in output_ids:
                candidate.setdefault("output_operation_ids", {}).setdefault(output_id, []).extend(
                    selected_operations
                )
            candidate["operation_node_ids"] = list(dict.fromkeys(candidate["operation_node_ids"]))
            candidate["boundary_operation_node_ids"] = list(dict.fromkeys(
                candidate["boundary_operation_node_ids"]
            ))
            for output_id in output_ids:
                candidate["output_operation_ids"][output_id] = list(dict.fromkeys(
                    candidate["output_operation_ids"][output_id]
                ))
            resolved_metrics.append(PlannedMetric.model_validate({
                **metric.model_dump(),
                "calculation_id": "report_semantic_scope",
                "output_node_ids": output_ids,
                "operation_node_ids": selected_operations,
                "query_node_ids": query_ids,
                "metric_source_fields": (
                    list(metric.claimed_metric_fields)
                    if verification == "attested"
                    else metric.metric_source_fields
                ),
                "lineage_verification": verification,
            }))

        report_outputs = [
            output
            for output in output_by_id.values()
            if output.get("artifact_type") == "report"
            and self._is_report_metric_output(output, lineage_index)
        ]
        unresolved_output_ids = {
            str(item.output_node_id or "") for item in unresolved if item.output_node_id
        }
        unresolved.extend(unresolved_duplicate_candidate_outputs)
        unresolved_output_ids.update(
            str(item.output_node_id or "")
            for item in unresolved_duplicate_candidate_outputs
            if item.output_node_id
        )
        planned_output_ids = {
            output_id
            for metric in resolved_metrics
            for output_id in metric.output_node_ids
        }
        for output in report_outputs:
            output_id = str(output.get("output_node_id") or "")
            anchor_id = str(output.get("source_anchor_id") or "")
            if (
                not output_id
                or output_id in planned_output_ids
                or output_id in unresolved_output_ids
                or anchor_id in non_metric_anchor_ids
                or output_id in non_metric_candidate_ids
            ):
                continue
            unresolved.append(UnresolvedEvidenceOutput(
                output_node_id=output_id,
                reason="Planner did not classify this verified report source anchor.",
            ))

        return EvidencePlan(
            metrics=resolved_metrics,
            unresolved_outputs=unresolved,
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=sorted(non_metric_candidate_ids),
        )

    @staticmethod
    def _is_grouping_dimension_output(
        output: dict[str, Any],
        lineage_index: dict[str, Any],
    ) -> bool:
        if output.get("structured_dimension_origin"):
            return True
        if PostAnalysisEvidenceCompiler._is_structured_ordinal_output(output):
            return True
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
    def _is_structured_ordinal_output(output: dict[str, Any]) -> bool:
        """Identify a table ordering column from both its role and rendered values."""
        if output.get("artifact_type") != "table":
            return False
        values = [
            int(value)
            for value in output.get("sample_values") or []
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value).is_integer()
            and float(value) > 0
        ]
        if not values or len(values) != len(output.get("sample_values") or []):
            return False
        if sorted(set(values)) != list(range(1, len(set(values)) + 1)):
            return False
        role_text = " ".join(filter(None, [
            str(output.get("field") or "").strip(),
            str(output.get("label") or "").strip(),
        ])).lower()
        return bool(re.search(
            r"(?:^|[\s_\-])(rank|ranking|priority|ordinal|order|sequence)(?:$|[\s_\-])"
            r"|(?:优先级|排名|排行|序号|顺序|排序)",
            role_text,
        ))

    @staticmethod
    def _is_report_metric_output(
        output: dict[str, Any],
        lineage_index: dict[str, Any],
    ) -> bool:
        if (
            output.get("presentation_ordinal")
            or output.get("presentation_temporal_literal")
            or output.get("presentation_comparison_threshold")
            or output.get("presentation_formula_coefficient")
            or output.get("presentation_business_identifier")
            or output.get("presentation_display_limit")
        ):
            return False
        if output.get("artifact_type") != "report":
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
        if operations and all(
            operation.get("semantic_kind") == "presentation"
            and not str(operation.get("query_node_id") or "")
            for operation in operations
        ):
            return False
        candidate_ids = {
            str(item)
            for item in output.get("candidate_operation_node_ids") or []
            if str(item)
        }
        candidate_operations = [
            operation
            for operation in lineage_index.get("operation_nodes") or []
            if str(operation.get("operation_node_id") or "") in candidate_ids
        ]
        parameter_controlled = any(
            {"analysis_parameters", "get_analysis_parameters"}.intersection(
                str(item) for item in operation.get("loads") or []
            )
            for operation in candidate_operations
        )
        if (
            parameter_controlled
            and not output.get("candidate_query_node_ids")
            and not output.get("runtime_iteration_boundary_required")
            and not any(
                str(operation.get("query_node_id") or "")
                for operation in candidate_operations
            )
        ):
            return False
        # Report output nodes only exist for numeric values located through an
        # AST-owned render template. If lineage is incomplete, retain the slot
        # as unresolved so compilation cannot report a false complete status.
        if output.get("numeric_hint"):
            return True
        boundary_ids = {
            str(item)
            for item in output.get("boundary_operation_node_ids") or []
            if str(item)
        }
        if not boundary_ids:
            return True
        # A numeric report slot may project one value from an already computed
        # query-backed result (for example the count on the top-ranked row).
        # The projection is the final selection boundary, while aggregation or
        # ranking lives in its ancestry. Explicit presentation literals were
        # removed above, so a proven query closure is sufficient here.
        if output.get("candidate_query_node_ids") and any(
            operation.get("semantic_kind") in {"aggregation", "formula", "ranking"}
            for operation in candidate_operations
        ):
            return True
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
        operation_by_id = {
            str(item.get("operation_node_id") or ""): item
            for item in lineage_index.get("operation_nodes") or []
            if str(item.get("operation_node_id") or "")
        }
        producers: dict[str, list[str]] = {}
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                target_name = str(target or "").strip()
                if target_name:
                    producers.setdefault(target_name, []).append(operation_id)

        def container_field_signature(target: Any) -> tuple[str, str] | None:
            """Return ``(container, field)`` for dict/list field access.

            Renderers commonly keep grouped values in a list of dictionaries,
            then interpolate ``summary[index]['count']`` into the report. The
            write side is represented as ``summary['count'] = value`` by the
            lineage index.  Their shared container/field identity is a local,
            syntax-level fact even though the list index is dynamic.
            """
            try:
                expression = ast.parse(str(target or ""), mode="eval").body
            except SyntaxError:
                return None
            if not isinstance(expression, ast.Subscript):
                return None
            field_node = expression.slice
            if not (
                isinstance(field_node, ast.Constant)
                and isinstance(field_node.value, str)
                and str(field_node.value).strip()
            ):
                return None
            container = expression.value
            while isinstance(container, ast.Subscript):
                container = container.value
            if not isinstance(container, ast.Name):
                return None
            return container.id, str(field_node.value).strip()

        for output in lineage_index.get("output_nodes") or []:
            ambiguous_input = bool(output.get("ambiguous_calculation_boundary"))
            if ambiguous_input and output.get("artifact_type") != "report":
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
            # Loop-rendered report fields can have an exact AST expression and
            # a calculation boundary while the broad candidate walk misses its
            # query list.  Derive that closure from the expression/boundary
            # operations themselves.  This is a local proof, not an LLM
            # recovery path, and keeps each rendered field's own boundary.
            direct_operation_ids = list(dict.fromkeys(
                str(item)
                for item in output.get("direct_operation_node_ids") or []
                if str(item) in operation_by_id
            ))
            if not boundaries:
                output_signature = container_field_signature(output.get("field"))
                if output_signature is not None:
                    boundaries = list(dict.fromkeys(
                        operation_id
                        for operation_id, operation in operation_by_id.items()
                        if any(
                            container_field_signature(target) == output_signature
                            for target in operation.get("targets") or []
                        )
                        and LineageIndexer._query_dependencies(
                            [operation_id], operation_by_id, producers
                        )
                    ))
            if not query_ids:
                query_ids = list(dict.fromkeys(
                    query_id
                    for operation_id in [*direct_operation_ids, *boundaries]
                    for query_id in LineageIndexer._query_dependencies(
                        [operation_id], operation_by_id, producers
                    )
                    if str(query_id)
                ))
            operation_ids = list(dict.fromkeys([
                *operation_ids,
                *boundaries,
                *direct_operation_ids,
            ]))
            if not boundaries or not query_ids or not operation_ids:
                continue
            # The fallback above is a deterministic syntax/dataflow proof. Keep
            # the recovered facts on the canonical output node as well as the
            # calculation candidate; otherwise the verified-report pass sees a
            # stale output and unnecessarily delegates an already-proven slot
            # to the semantic recovery planner.
            output["boundary_operation_node_ids"] = list(boundaries)
            output["candidate_query_node_ids"] = list(query_ids)
            output["candidate_operation_node_ids"] = list(operation_ids)
            if PostAnalysisEvidenceCompiler._is_grouping_dimension_output(output, lineage_index):
                continue
            parameter_bindings, metric_source_fields = (
                PostAnalysisEvidenceCompiler._resolve_literal_parameter_bindings(
                    output,
                    boundaries=boundaries,
                    operation_by_id=operation_by_id,
                    literal_iteration_bindings=(
                        lineage_index.get("literal_iteration_bindings") or []
                    ),
                )
            )
            proven_query_fields = list(dict.fromkeys(
                str(target).strip()
                for operation_id in operation_ids
                for operation in [operation_by_id.get(operation_id) or {}]
                if operation.get("kind") in {"query_projection", "source_field"}
                and str(operation.get("query_node_id") or "") in query_ids
                for target in operation.get("targets") or []
                if str(target).strip()
            ))
            boundary_kinds = {
                str((operation_by_id.get(operation_id) or {}).get("semantic_kind") or "")
                for operation_id in boundaries
            }
            if (
                not metric_source_fields
                and boundary_kinds == {"formula"}
                and proven_query_fields
            ):
                metric_source_fields = proven_query_fields
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
                "parameter_bindings": parameter_bindings,
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
                "parameter_bindings": parameter_bindings,
                "metric_source_fields": metric_source_fields,
                "available_metric_fields": [],
                "ambiguous_input": False,
                "operation_query_ids": {},
                "operation_family_signatures": {},
                "operation_write_signatures": {},
                "operation_semantic_kinds": {},
            })
            candidate["ambiguous_input"] = bool(
                candidate.get("ambiguous_input") or ambiguous_input
            )
            operation_query_ids = candidate.setdefault("operation_query_ids", {})
            for operation_id in boundaries:
                operation = operation_by_id.get(operation_id) or {}
                reachable_queries = LineageIndexer._query_dependencies(
                    [operation_id],
                    operation_by_id,
                    producers,
                )
                operation_query_ids[operation_id] = list(dict.fromkeys([
                    *operation_query_ids.get(operation_id, []),
                    *reachable_queries,
                ]))
                candidate["operation_family_signatures"][operation_id] = (
                    LineageIndexer._operation_family_signature(operation)
                )
                candidate["operation_semantic_kinds"][operation_id] = str(
                    operation.get("semantic_kind") or operation.get("kind") or ""
                )
                write_signature = LineageIndexer._operation_write_signature(operation)
                if write_signature:
                    candidate["operation_write_signatures"][operation_id] = write_signature
            candidate["output_node_ids"].append(str(output.get("output_node_id") or ""))
            candidate["available_metric_fields"] = list(dict.fromkeys([
                *candidate.get("available_metric_fields", []),
                *metric_source_fields,
                *[
                    str(field).strip()
                    for field in output.get("lineage_fields") or []
                    if str(field).strip()
                ],
                *(
                    [str(output.get("field") or "").strip()]
                    if str(output.get("field") or "").strip()
                    else []
                ),
            ]))
            candidate["operation_node_ids"] = list(dict.fromkeys([
                *candidate["operation_node_ids"],
                *operation_ids,
            ]))
        return grouped

    @staticmethod
    def _build_report_semantic_candidates(
        lineage_index: dict[str, Any],
        proven_candidates: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Build one report-wide semantic scope; AST lineage remains advisory."""
        operation_by_id = {
            str(item.get("operation_node_id") or ""): item
            for item in lineage_index.get("operation_nodes") or []
            if str(item.get("operation_node_id") or "")
        }
        producers: dict[str, list[str]] = {}
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                target_name = str(target or "").strip()
                if target_name:
                    producers.setdefault(target_name, []).append(operation_id)

        query_ids_by_operation = {
            operation_id: LineageIndexer._query_dependencies(
                [operation_id],
                operation_by_id,
                producers,
            )
            for operation_id in operation_by_id
        }
        selectable_operations = {
            operation_id: operation
            for operation_id, operation in operation_by_id.items()
            if query_ids_by_operation.get(operation_id)
            and operation.get("kind") != "validated_query"
            and operation.get("semantic_kind") != "presentation"
        }
        report_outputs = [
            output
            for output in lineage_index.get("output_nodes") or []
            if output.get("artifact_type") == "report"
            and PostAnalysisEvidenceCompiler._is_report_metric_output(output, lineage_index)
            and not PostAnalysisEvidenceCompiler._is_grouping_dimension_output(
                output,
                lineage_index,
            )
        ]
        source_anchors = [
            anchor
            for anchor in lineage_index.get("report_source_anchors") or []
            if str(anchor.get("anchor_id") or "")
        ]
        operation_ids = list(selectable_operations)
        query_ids = list(dict.fromkeys(
            query_id
            for operation_id in operation_ids
            for query_id in query_ids_by_operation.get(operation_id) or []
            if str(query_id)
        ))
        if not source_anchors or not operation_ids or not query_ids:
            return {}

        report_output_by_id = {
            str(output.get("output_node_id") or ""): output
            for output in report_outputs
            if str(output.get("output_node_id") or "")
        }
        local_hints: dict[str, dict[str, Any]] = {}
        for calculation_id, candidate in proven_candidates.items():
            for output_id in candidate.get("output_node_ids") or []:
                output = report_output_by_id.get(str(output_id)) or {}
                local_hints[str(output_id)] = {
                    "calculation_id": calculation_id,
                    "local_boundary_key": _stable_json({
                        "operation_node_ids": sorted(
                            str(item)
                            for item in candidate.get("boundary_operation_node_ids") or []
                            if str(item)
                        ),
                        "query_node_ids": sorted(
                            str(item)
                            for item in candidate.get("query_node_ids") or []
                            if str(item)
                        ),
                        "metric_source_fields": sorted(
                            str(item)
                            for item in candidate.get("metric_source_fields") or []
                            if str(item)
                        ),
                        "parameter_bindings": candidate.get("parameter_bindings") or {},
                    }),
                    "operation_node_ids": candidate.get("boundary_operation_node_ids") or [],
                    "query_node_ids": candidate.get("query_node_ids") or [],
                    "metric_source_fields": candidate.get("metric_source_fields") or [],
                    "parameter_bindings": candidate.get("parameter_bindings") or {},
                    "output_lineage_fields": output.get("lineage_fields") or [],
                    "ambiguous": bool(candidate.get("ambiguous_input")),
                }

        calculation_id = "report_semantic_scope"
        output_operation_ids = {
            str(output.get("output_node_id") or ""): [
                operation_id
                for operation_id in dict.fromkeys(
                    operation_ids if output.get("posthoc_report_candidate") else [
                        *(output.get("candidate_operation_node_ids") or []),
                        *(output.get("boundary_operation_node_ids") or []),
                        *(output.get("direct_operation_node_ids") or []),
                    ]
                )
                if operation_id in selectable_operations
            ]
            for output in report_outputs
            if str(output.get("output_node_id") or "")
        }
        return {calculation_id: {
            "calculation_id": calculation_id,
            "boundary_operation_node_id": operation_ids[0],
            "boundary_operation_node_ids": operation_ids,
            "output_node_ids": [
                str(output.get("output_node_id") or "") for output in report_outputs
                if str(output.get("output_node_id") or "")
            ],
            "operation_node_ids": operation_ids,
            "query_node_ids": query_ids,
            "parameter_bindings": {},
            "metric_source_fields": [],
            "available_metric_fields": list(dict.fromkeys(
                str(target).strip()
                for operation_id in operation_ids
                for target in (operation_by_id.get(operation_id) or {}).get("targets") or []
                if str(target).strip()
            )),
            "ambiguous_input": False,
            "recovery_candidate": True,
            "semantic_scope": True,
            "source_anchors": source_anchors,
            "local_lineage_hints": local_hints,
            "output_operation_ids": output_operation_ids,
            "operation_query_ids": {
                operation_id: query_ids_by_operation.get(operation_id) or []
                for operation_id in operation_ids
            },
            "operation_family_signatures": {
                operation_id: LineageIndexer._operation_family_signature(operation)
                for operation_id, operation in selectable_operations.items()
            },
            "operation_write_signatures": {
                operation_id: signature
                for operation_id in operation_ids
                if (
                    signature := LineageIndexer._operation_write_signature(
                        operation_by_id.get(operation_id) or {}
                    )
                )
            },
            "operation_semantic_kinds": {
                operation_id: str(
                    (operation_by_id.get(operation_id) or {}).get("semantic_kind")
                    or (operation_by_id.get(operation_id) or {}).get("kind")
                    or ""
                )
                for operation_id in operation_ids
            },
        }}

    @staticmethod
    def _resolve_literal_parameter_bindings(
        output: dict[str, Any],
        *,
        boundaries: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        literal_iteration_bindings: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        dynamic_field_names: set[str] = set()
        loaded_parameter_names: set[str] = set()
        for operation_id in boundaries:
            source = str((operation_by_id.get(operation_id) or {}).get("source") or "")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            loaded_parameter_names.update(
                item.id
                for item in ast.walk(tree)
                if isinstance(item, ast.Name)
                and isinstance(item.ctx, ast.Load)
            )
            dynamic_field_names.update(
                item.slice.id
                for item in ast.walk(tree)
                if isinstance(item, ast.Subscript)
                and isinstance(item.slice, ast.Name)
            )
        if not dynamic_field_names:
            return {}, []

        rendered_values = {
            str(value).strip()
            for value in (output.get("rendered_slot_bindings") or {}).values()
            if str(value).strip()
        }
        if not rendered_values:
            return {}, []

        for group in literal_iteration_bindings:
            rows = [item for item in group.get("rows") or [] if isinstance(item, dict)]
            group_names = set().union(*(set(item) for item in rows)) if rows else set()
            selector_names = sorted(dynamic_field_names.intersection(group_names))
            if not selector_names:
                continue
            matched_rows = [
                row
                for row in rows
                if any(
                    str(value).strip() in rendered_values
                    for name, value in row.items()
                    if name not in selector_names and str(value).strip()
                )
            ]
            if len(matched_rows) != 1:
                continue
            selected = matched_rows[0]
            computational_parameter_names = sorted(
                loaded_parameter_names.intersection(selected)
            )
            parameter_bindings = {
                name: selected[name]
                for name in computational_parameter_names
            }
            metric_source_fields = [
                str(selected[name]).strip()
                for name in selector_names
                if str(selected.get(name) or "").strip()
            ]
            return parameter_bindings, metric_source_fields
        return {}, []

    @staticmethod
    def _filter_candidates_by_artifact_type(
        candidates: dict[str, dict[str, Any]],
        lineage_index: dict[str, Any],
        artifact_type: str | set[str],
    ) -> dict[str, dict[str, Any]]:
        artifact_types = (
            {artifact_type} if isinstance(artifact_type, str) else set(artifact_type)
        )
        output_by_id = {
            str(item.get("output_node_id") or ""): item
            for item in lineage_index.get("output_nodes") or []
        }
        result: dict[str, dict[str, Any]] = {}
        for calculation_id, candidate in candidates.items():
            output_ids = [
                str(output_id)
                for output_id in candidate.get("output_node_ids") or []
                if (output_by_id.get(str(output_id)) or {}).get("artifact_type") in artifact_types
            ]
            if output_ids:
                result[calculation_id] = {**candidate, "output_node_ids": output_ids}
        return result

    @staticmethod
    def _build_verified_report_completion_plan(
        report_plan: EvidencePlan,
        candidates: dict[str, dict[str, Any]],
        lineage_index: dict[str, Any],
    ) -> EvidencePlan:
        """Fill planner omissions only when local lineage is already proven.

        The report planner owns semantic discovery. This completion step does
        not infer new metrics from text; it only prevents a verified report
        output from losing evidence because the planner omitted an anchor that
        AST + QuerySnapshot already proved reachable.
        """
        output_by_id = {
            str(item.get("output_node_id") or ""): item
            for item in lineage_index.get("output_nodes") or []
            if str(item.get("output_node_id") or "")
        }
        claimed = {
            str(output_id)
            for metric in report_plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id)
        }
        unresolved = {
            str(item.output_node_id or "")
            for item in report_plan.unresolved_outputs
            if str(item.output_node_id or "")
        }
        metrics: list[PlannedMetric] = []
        for calculation_id, candidate in candidates.items():
            # Local completion is only authoritative when one calculation
            # boundary is already proven. Dynamic report loops can render the
            # same placeholder from several independent aggregations; those
            # candidates must remain visible to semantic resolution instead of
            # being prematurely collapsed into one "verified" metric.
            if candidate.get("ambiguous_input") or candidate.get(
                "ambiguity_requires_resolution"
            ):
                continue
            output_ids = [
                str(output_id)
                for output_id in candidate.get("output_node_ids") or []
                if (output := output_by_id.get(str(output_id)))
                and output.get("artifact_type") == "report"
                and str(output_id) not in claimed
                and str(output_id) not in unresolved
                and output.get("boundary_operation_node_ids")
                and output.get("candidate_query_node_ids")
                and output.get("candidate_operation_node_ids")
                and PostAnalysisEvidenceCompiler._is_report_metric_output(
                    output,
                    lineage_index,
                )
                and not PostAnalysisEvidenceCompiler._is_grouping_dimension_output(
                    output,
                    lineage_index,
                )
            ]
            if not output_ids:
                continue
            outputs = [output_by_id[output_id] for output_id in output_ids]
            source_anchors = [
                ReportSourceAnchorRef(anchor_id=anchor_id)
                for anchor_id in list(dict.fromkeys(
                    str(output.get("source_anchor_id") or "")
                    for output in outputs
                    if str(output.get("source_anchor_id") or "")
                ))
            ]
            metric_fields = list(dict.fromkeys(
                candidate.get("metric_source_fields")
                or [
                    str(field).strip()
                    for output in outputs
                    for field in output.get("lineage_fields") or []
                    if str(field).strip()
                ]
            ))
            labels = list(dict.fromkeys(
                label
                for output in outputs
                if (label := PostAnalysisEvidenceCompiler._report_output_business_name(output))
            ))
            operation_ids = [
                str(item)
                for item in candidate.get("operation_node_ids") or []
                if str(item)
            ]
            operation_query_ids = (
                candidate.get("operation_query_ids")
                if isinstance(candidate.get("operation_query_ids"), dict)
                else {}
            )
            reachable_query_ids = list(dict.fromkeys(
                str(query_id)
                for operation_id in operation_ids
                for query_id in operation_query_ids.get(operation_id) or []
                if str(query_id)
            ))
            allowed_query_ids = {
                str(query_id)
                for query_id in candidate.get("query_node_ids") or []
                if str(query_id)
            }
            verified_query_ids = [
                query_id
                for query_id in reachable_query_ids
                if query_id in allowed_query_ids
            ]
            metrics.append(PlannedMetric(
                calculation_id=str(calculation_id),
                output_node_ids=output_ids,
                source_anchors=source_anchors,
                operation_node_ids=operation_ids,
                query_node_ids=[
                    str(item)
                    for item in (
                        verified_query_ids
                        or candidate.get("query_node_ids")
                        or []
                    )
                    if str(item)
                ],
                metric_source_fields=metric_fields,
                parameter_bindings=(
                    candidate.get("parameter_bindings")
                    if isinstance(candidate.get("parameter_bindings"), dict)
                    else {}
                ),
                metric_key=f"verified_report_{_digest(str(calculation_id), 12)}",
                business_name=labels[0] if labels else "报告数值",
                business_explanation=(
                    PostAnalysisEvidenceCompiler._verified_metric_explanation(
                        labels[0] if labels else "报告数值"
                    )
                ),
                value_type="number",
                display_unit=str(outputs[0].get("display_unit_hint") or ""),
            ))
        return EvidencePlan(metrics=metrics)

    @staticmethod
    def _canonical_report_number(value: Any) -> Decimal | None:
        text = str(value or "").strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _report_output_unit(output: dict[str, Any]) -> str:
        explicit = str(output.get("display_unit_hint") or "").strip()
        if explicit:
            return explicit
        context = output.get("display_context") or {}
        inferred = LineageIndexer._explicit_display_unit(
            str(context.get("token_suffix") or ""),
            str(output.get("display_suffix") or ""),
        )
        if inferred:
            return inferred
        # Markdown emphasis often sits between the numeric token and its unit
        # (for example ``**1** records``).  One immediate CJK counter character
        # is enough to distinguish repeated values without guessing a business
        # unit from the rest of the sentence.
        suffix = str(context.get("token_suffix") or output.get("display_suffix") or "")
        suffix = re.sub(r"^[\s*_`~)\]}]+", "", suffix)
        match = re.match(r"(%|[A-Za-z]{1,12}|[\u4e00-\u9fff])", suffix)
        return match.group(1) if match else ""

    @classmethod
    def _report_output_business_name(cls, output: dict[str, Any]) -> str:
        """Name a report metric from its rendered business context.

        The syntactic label can be a Python expression. It remains useful in
        lineage metadata, but it must not leak into the user-facing Evidence
        name when the rendered report already provides a business label.
        """

        def plain(value: Any) -> str:
            text = re.sub(r"[*_`~#]", " ", str(value or ""))
            text = re.sub(r"^\s*[-+>]\s*", "", text)
            return re.sub(r"\s+", " ", text).strip()

        def without_display_number(value: str) -> str:
            text = re.sub(
                r"[-+]?\d[\d,]*(?:\.\d+)?(?:%|[\u4e00-\u9fff]{0,3})?",
                " ",
                value,
            )
            return re.sub(r"\s+", " ", text).strip()

        context = output.get("display_context") or {}
        heading = plain(context.get("heading") or "").strip(" -—:：")
        column_label = plain(context.get("column_label") or "").strip(" -—:：")
        if column_label and column_label != heading and len(column_label) <= 40:
            return column_label

        line = plain(context.get("line") or output.get("rendered_context") or "")
        prefix = plain(
            context.get("token_prefix") or output.get("display_prefix") or ""
        ).strip(" -—")
        suffix = plain(
            context.get("token_suffix") or output.get("display_suffix") or ""
        ).strip()
        topic = ""
        if line:
            topic_candidate = plain(re.split(r"[:：]", line, maxsplit=1)[0]).strip(" -—")
            if topic_candidate and len(topic_candidate) <= 36:
                topic = topic_candidate

        segments = [
            cleaned
            for item in re.split(r"[:：,，;；。.!！?？()（）、]", prefix)
            if (cleaned := plain(item).strip(" -—"))
        ]
        tail = without_display_number(segments[-1]) if segments else ""
        generic_tail = tail in {
            "累计", "涉及", "占", "约", "为", "达到", "共有", "合计",
        }
        unit = cls._report_output_unit(output)
        suffix_subject = ""
        suffix_match = re.match(
            r"(?:%|[个条张次台套件笔份名项组批]*)([A-Za-z\u4e00-\u9fff]{2,12})",
            suffix,
        )
        if suffix_match:
            suffix_subject = suffix_match.group(1)
        unit_noun = {
            "%": "占比",
            "次": "次数",
            "张": "数量",
            "个": "数量",
            "条": "数量",
            "台": "台数",
            "份": "份数",
            "元": "金额",
        }.get(unit, suffix_subject or (f"{unit}数" if unit else "数值"))

        if generic_tail:
            previous = without_display_number(segments[-2]) if len(segments) > 1 else ""
            subject = topic or previous or heading
            name = f"{subject}{tail}{unit_noun}" if subject else f"{tail}{unit_noun}"
        else:
            name = tail or topic or heading

        name = plain(name).strip(" -—:：,，;；。.")
        if not name or name == heading:
            name = plain(f"{heading} {unit_noun}").strip() or "报告数值"
        return name[:80]

    @staticmethod
    def _verified_metric_explanation(business_name: str) -> str:
        """Describe the business calculation without exposing compiler internals."""
        name = str(business_name or "").strip()
        if re.search(r"[\u4e00-\u9fff]", name):
            return f"按本轮确认的数据范围和计算口径统计{name}。"
        return f"Calculated {name} using the confirmed data scope and analysis definition."

    @staticmethod
    def _report_context_features(output: dict[str, Any]) -> set[str]:
        context = output.get("display_context") or {}
        text = " ".join(filter(None, [
            str(output.get("label") or ""),
            str(output.get("field") or ""),
            str(context.get("heading") or ""),
            str(context.get("column_label") or ""),
            str(context.get("token_prefix") or "")[-80:],
            str(context.get("token_suffix") or "")[:80],
        ])).lower()
        text = re.sub(r"[-+]?\d[\d,.]*(?:[eE][-+]?\d+)?", " ", text)
        features = {
            token
            for token in re.findall(r"[a-z_][a-z0-9_]{1,}|[\u4e00-\u9fff]{2,}", text)
            if len(token) >= 2
        }

        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            features.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
        return features

    @classmethod
    def _repeated_report_metric_matches(
        cls,
        output: dict[str, Any],
        source_outputs: list[tuple[PlannedMetric, dict[str, Any]]],
    ) -> tuple[PlannedMetric, dict[str, Any]] | None:
        value = cls._canonical_report_number(output.get("text"))
        if value is None:
            return None
        unit = cls._report_output_unit(output)
        features = cls._report_context_features(output)
        scored: list[tuple[int, PlannedMetric, dict[str, Any]]] = []
        for metric, source in source_outputs:
            if cls._canonical_report_number(source.get("text")) != value:
                continue
            source_unit = cls._report_output_unit(source) or str(metric.display_unit or "")
            if unit and source_unit and unit != source_unit:
                continue
            shared = features.intersection(cls._report_context_features(source))
            if not shared:
                continue
            business_key = re.sub(
                r"[^a-z0-9_\u4e00-\u9fff]+",
                "",
                f"{metric.metric_key}{metric.business_name}".lower(),
            )
            context_key = re.sub(
                r"[^a-z0-9_\u4e00-\u9fff]+",
                "",
                "".join(features),
            )
            score = len(shared) + (4 if business_key and business_key in context_key else 0)
            scored.append((score, metric, source))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1], scored[0][2]

    @staticmethod
    def _expression_root_and_field(value: Any) -> tuple[str, str] | None:
        try:
            expression = ast.parse(str(value or ""), mode="eval").body
        except SyntaxError:
            return None
        if not isinstance(expression, ast.Subscript):
            return None
        field_node = expression.slice
        if not (
            isinstance(field_node, ast.Constant)
            and isinstance(field_node.value, str)
            and str(field_node.value).strip()
        ):
            return None
        container = expression.value
        while isinstance(container, (ast.Subscript, ast.Attribute)):
            container = container.value
        if not isinstance(container, ast.Name):
            return None
        return container.id, str(field_node.value).strip()

    @classmethod
    def _structured_report_metric_match(
        cls,
        output: dict[str, Any],
        source_outputs: list[tuple[PlannedMetric, dict[str, Any]]],
        lineage_index: dict[str, Any],
    ) -> tuple[PlannedMetric, dict[str, Any]] | None:
        """Reuse a verified structured column for the same report frame field.

        A report often iterates a DataFrame while a table renders a projected
        copy of that same frame. The table already proves one column-level
        calculation boundary. Reusing it is deterministic when the report row
        field is identical and the two frames have a direct derivation path.
        """
        report_identity = cls._expression_root_and_field(output.get("field"))
        if report_identity is None:
            return None
        report_row, report_field = report_identity
        operation_by_id = {
            str(item.get("operation_node_id") or ""): item
            for item in lineage_index.get("operation_nodes") or []
            if str(item.get("operation_node_id") or "")
        }

        report_frames: set[str] = set()
        for operation_id in output.get("candidate_operation_node_ids") or []:
            operation = operation_by_id.get(str(operation_id)) or {}
            if operation.get("kind") != "container_iteration":
                continue
            targets = {str(item) for item in operation.get("targets") or []}
            if report_row not in targets:
                continue
            report_frames.update(
                str(item).strip()
                for item in operation.get("loads") or []
                if str(item).strip() and str(item).strip() != report_row
            )
        if not report_frames:
            return None

        def root_name(value: Any) -> str:
            text = str(value or "").strip()
            match = re.match(r"[A-Za-z_]\w*", text)
            return match.group(0) if match else ""

        frame_dependencies: dict[str, set[str]] = {}
        for operation in operation_by_id.values():
            if operation.get("semantic_kind") not in {
                "presentation", "transformation", "ranking",
            }:
                continue
            targets = {
                root_name(item) for item in operation.get("targets") or []
                if root_name(item)
            }
            loads = {
                root_name(item) for item in operation.get("loads") or []
                if root_name(item)
            }
            for target in targets:
                frame_dependencies.setdefault(target, set()).update(loads - {target})

        def derives_from(source: str, target: str) -> bool:
            pending = [source]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(frame_dependencies.get(current, set()) - visited)
            return False

        matches: dict[str, tuple[PlannedMetric, dict[str, Any]]] = {}
        for metric, source in source_outputs:
            source_field = str(source.get("field") or "").strip()
            source_fields = {
                source_field,
                *(str(item).strip() for item in source.get("lineage_fields") or []),
            }
            if report_field not in source_fields:
                continue
            source_frame = str(source.get("source_frame") or "").strip()
            if not source_frame or not any(
                derives_from(source_frame, report_frame)
                or derives_from(report_frame, source_frame)
                for report_frame in report_frames
            ):
                continue
            matches[str(metric.calculation_id or "")] = (metric, source)
        if len(matches) != 1:
            return None
        return next(iter(matches.values()))

    @staticmethod
    def _empty_result_operation(
        snapshot: AnalysisExecutionSnapshot,
        output: dict[str, Any],
        lineage_index: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]] | None:
        if PostAnalysisEvidenceCompiler._canonical_report_number(output.get("text")) != Decimal(0):
            return None
        context = output.get("display_context") or {}
        rendered_line = str(context.get("line") or output.get("rendered_context") or "")
        try:
            tree = ast.parse(snapshot.generated_code or "")
        except SyntaxError:
            return None

        parent_by_node: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parent_by_node[child] = parent

        operation_by_id = {
            str(item.get("operation_node_id") or ""): item
            for item in lineage_index.get("operation_nodes") or []
            if str(item.get("operation_node_id") or "")
        }
        producers: dict[str, list[str]] = {}
        for operation_id, operation in operation_by_id.items():
            for target in operation.get("targets") or []:
                for key in _producer_keys(target):
                    producers.setdefault(key, []).append(operation_id)

        normalized_line = re.sub(r"\s+|[*_`]", "", rendered_line)
        matches: list[tuple[int, ast.If]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            literal = str(node.value)
            if "0" not in literal:
                continue
            normalized_literal = re.sub(r"\s+|[*_`]", "", literal)
            if not normalized_literal or not (
                normalized_literal in normalized_line
                or normalized_line in normalized_literal
            ):
                continue
            ancestor = parent_by_node.get(node)
            while ancestor is not None and not isinstance(ancestor, ast.If):
                ancestor = parent_by_node.get(ancestor)
            if not isinstance(ancestor, ast.If):
                continue
            condition_operation_ids = list(dict.fromkeys(
                operation_id
                for name in _loaded_names(ancestor.test)
                for operation_id in producers.get(name, [])
            ))
            condition_sources = [
                ast.get_source_segment(snapshot.generated_code, ancestor.test) or ast.unparse(ancestor.test),
                *[
                    str((operation_by_id.get(operation_id) or {}).get("source") or "")
                    for operation_id in condition_operation_ids
                ],
            ]
            empty_check = " ".join(condition_sources).lower()
            if not (
                ".empty" in empty_check
                or re.search(r"\blen\s*\(", empty_check)
                or ".shape[0]" in empty_check
                or "is none" in empty_check
            ):
                continue
            query_ids = LineageIndexer._query_dependencies(
                condition_operation_ids,
                operation_by_id,
                producers,
            )
            if not query_ids:
                continue
            matches.append((int(getattr(ancestor, "lineno", 0) or 0), ancestor))
        if len(matches) != 1:
            return None
        line, branch = matches[0]
        source = ast.get_source_segment(snapshot.generated_code, branch.test) or ast.unparse(branch.test)
        query_ids = LineageIndexer._query_dependencies(
            list(dict.fromkeys(
                operation_id
                for name in _loaded_names(branch.test)
                for operation_id in producers.get(name, [])
            )),
            operation_by_id,
            producers,
        )
        operation_id = f"op_empty_result_{line}_{_digest(source, 10)}"
        return ({
            "operation_node_id": operation_id,
            "line": line,
            "kind": "calculation",
            "semantic_kind": "empty_result",
            "query_node_id": "",
            "targets": [],
            "loads": _loaded_names(branch.test),
            "source": source[:1200],
            "posthoc_empty_result": True,
        }, query_ids)

    @classmethod
    def _complete_proven_report_positions(
        cls,
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        plan: EvidencePlan,
        candidates: dict[str, dict[str, Any]],
    ) -> tuple[EvidencePlan, dict[str, dict[str, Any]]]:
        """Bind report positions only when their calculation boundary is proven."""
        output_by_id = {
            str(item.get("output_node_id") or ""): item
            for item in lineage_index.get("output_nodes") or []
            if str(item.get("output_node_id") or "")
        }
        claimed = {
            str(output_id)
            for metric in plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id)
        }
        source_outputs = [
            (metric, output_by_id[str(output_id)])
            for metric in plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id) in output_by_id
            and output_by_id[str(output_id)].get("artifact_type") == "report"
            and not output_by_id[str(output_id)].get("posthoc_report_candidate")
        ]
        structured_source_outputs = [
            (metric, output_by_id[str(output_id)])
            for metric in plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id) in output_by_id
            and output_by_id[str(output_id)].get("artifact_type") in {"chart", "table"}
        ]
        added_metrics: list[PlannedMetric] = []
        added_candidates: dict[str, dict[str, Any]] = {}
        filled: set[str] = set()
        for output_id, output in output_by_id.items():
            if (
                output_id in claimed
                or output.get("artifact_type") != "report"
                or not output.get("posthoc_report_candidate")
                or not cls._is_report_metric_output(output, lineage_index)
            ):
                continue
            repeated = cls._repeated_report_metric_matches(output, source_outputs)
            if repeated is not None:
                source_metric, _ = repeated
                candidate = candidates.get(str(source_metric.calculation_id or ""))
                if candidate is None:
                    continue
                candidate.setdefault("output_node_ids", []).append(output_id)
                candidate["output_node_ids"] = list(dict.fromkeys(candidate["output_node_ids"]))
                added_metrics.append(source_metric.model_copy(update={
                    "output_node_ids": [output_id],
                    "source_anchors": [],
                    "report_candidate_ids": [
                        str(output.get("report_candidate_group_id") or output_id)
                    ],
                }))
                filled.add(output_id)
                continue

            structured = cls._structured_report_metric_match(
                output,
                structured_source_outputs,
                lineage_index,
            )
            if structured is not None:
                source_metric, _ = structured
                candidate = candidates.get(str(source_metric.calculation_id or ""))
                if candidate is None:
                    continue
                candidate.setdefault("output_node_ids", []).append(output_id)
                candidate["output_node_ids"] = list(dict.fromkeys(candidate["output_node_ids"]))
                added_metrics.append(source_metric.model_copy(update={
                    "output_node_ids": [output_id],
                    "source_anchors": [],
                    "report_candidate_ids": [
                        str(output.get("report_candidate_group_id") or output_id)
                    ],
                }))
                filled.add(output_id)
                continue

            empty_result = cls._empty_result_operation(snapshot, output, lineage_index)
            if empty_result is None:
                continue
            operation, query_ids = empty_result
            operation_id = str(operation["operation_node_id"])
            if not any(
                str(item.get("operation_node_id") or "") == operation_id
                for item in lineage_index.get("operation_nodes") or []
            ):
                lineage_index.setdefault("operation_nodes", []).append(operation)
            calculation_id = f"empty_report_{_digest(output_id + _stable_json(query_ids), 16)}"
            candidate = {
                "calculation_id": calculation_id,
                "boundary_operation_node_ids": [operation_id],
                "operation_node_ids": [operation_id],
                "output_node_ids": [output_id],
                "query_node_ids": query_ids,
                "metric_source_fields": [],
                "available_metric_fields": [],
                "operation_query_ids": {operation_id: query_ids},
                "operation_semantic_kinds": {operation_id: "empty_result"},
                "posthoc_empty_result": True,
            }
            added_candidates[calculation_id] = candidate
            label = str(output.get("label") or "").strip() or "No matching records"
            added_metrics.append(PlannedMetric(
                calculation_id=calculation_id,
                report_candidate_ids=[
                    str(output.get("report_candidate_group_id") or output_id)
                ],
                output_node_ids=[output_id],
                operation_node_ids=[operation_id],
                query_node_ids=query_ids,
                metric_key=f"empty_result_{_digest(output_id, 12)}",
                business_name=label,
                business_explanation=(
                    "The confirmed query returned no matching records for the stated scope."
                ),
                value_type="integer",
                display_unit=cls._report_output_unit(output),
            ))
            filled.add(output_id)

        if not added_metrics:
            return plan, added_candidates
        return EvidencePlan(
            metrics=[*plan.metrics, *added_metrics],
            unresolved_outputs=[
                item for item in plan.unresolved_outputs
                if str(item.output_node_id or "") not in filled
            ],
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=[
                candidate_id
                for candidate_id in plan.non_metric_report_candidate_ids
                if not any(
                    str((output_by_id.get(output_id) or {}).get("report_candidate_group_id") or output_id)
                    == str(candidate_id)
                    for output_id in filled
                )
            ],
        ), added_candidates

    @staticmethod
    def _isolate_invalid_metrics(
        plan: EvidencePlan,
        candidates: dict[str, dict[str, Any]],
    ) -> EvidencePlan:
        """Keep independently valid boundaries when one planned metric is invalid.

        Evidence compilation is post-processing. A semantic planner miss for
        one report position must not erase deterministic chart/table evidence
        or other locally proven report metrics. Boundary validation remains
        strict; rejected outputs are surfaced as unresolved instead.
        """
        valid_metrics: list[PlannedMetric] = []
        unresolved = list(plan.unresolved_outputs)
        for metric in plan.metrics:
            errors = PostAnalysisEvidenceCompiler._plan_reference_errors(
                EvidencePlan(metrics=[metric]),
                candidates,
                require_complete=False,
            )
            if not errors:
                valid_metrics.append(metric)
                continue
            reason = "; ".join(errors[:5])
            unresolved.extend(
                UnresolvedEvidenceOutput(
                    output_node_id=str(output_id),
                    reason=reason,
                )
                for output_id in metric.output_node_ids
                if str(output_id)
            )
        return EvidencePlan(
            metrics=valid_metrics,
            unresolved_outputs=unresolved,
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=plan.non_metric_report_candidate_ids,
        )

    @staticmethod
    def _filter_plan_claims(
        plan: EvidencePlan,
        *,
        excluded_output_ids: set[str],
        allowed_output_ids: set[str] | None = None,
    ) -> EvidencePlan:
        if not excluded_output_ids and allowed_output_ids is None:
            return plan
        metrics: list[PlannedMetric] = []
        for metric in plan.metrics:
            output_ids = [
                str(output_id)
                for output_id in metric.output_node_ids
                if (
                    str(output_id)
                    and str(output_id) not in excluded_output_ids
                    and (
                        allowed_output_ids is None
                        or str(output_id) in allowed_output_ids
                    )
                )
            ]
            if not output_ids:
                continue
            metrics.append(metric.model_copy(update={"output_node_ids": output_ids}))
        unresolved_outputs = [
            item
            for item in plan.unresolved_outputs
            if (
                str(item.output_node_id or "") not in excluded_output_ids
                and (
                    not item.output_node_id
                    or allowed_output_ids is None
                    or str(item.output_node_id) in allowed_output_ids
                )
            )
        ]
        return EvidencePlan(
            metrics=metrics,
            unresolved_outputs=unresolved_outputs,
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=plan.non_metric_report_candidate_ids,
        )

    @staticmethod
    def _build_structured_evidence_plan(
        candidates: dict[str, dict[str, Any]],
        lineage_index: dict[str, Any],
    ) -> EvidencePlan:
        output_by_id = {
            str(item.get("output_node_id") or ""): item
            for item in lineage_index.get("output_nodes") or []
        }
        metrics: list[PlannedMetric] = []
        for calculation_id, candidate in candidates.items():
            output_ids = [
                str(output_id)
                for output_id in candidate.get("output_node_ids") or []
                if (output_by_id.get(str(output_id)) or {}).get("artifact_type") in {"chart", "table"}
            ]
            if not output_ids:
                continue
            outputs = [output_by_id[output_id] for output_id in output_ids]
            metric_fields = list(dict.fromkeys(
                candidate.get("metric_source_fields")
                or [
                    str(field).strip()
                    for output in outputs
                    for field in (
                        output.get("lineage_fields")
                        or ([output.get("field")] if output.get("field") else [])
                    )
                    if str(field).strip()
                ]
            ))
            label = next((
                str(output.get("label") or output.get("field") or "").strip()
                for output in outputs
                if str(output.get("label") or output.get("field") or "").strip()
            ), "数值指标")
            unit = next((
                str(output.get("display_unit_hint") or "").strip()
                for output in outputs
                if str(output.get("display_unit_hint") or "").strip()
            ), "")
            samples = [
                value
                for output in outputs
                for value in output.get("sample_values") or []
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            value_type = "number"
            if unit == "%":
                value_type = "percentage"
            elif unit in {"元", "万元", "亿元", "CNY", "RMB"}:
                value_type = "currency"
            elif samples and all(float(value).is_integer() for value in samples):
                value_type = "integer"
            operation_ids = list(
                candidate.get("boundary_operation_node_ids") or []
            )
            operation_query_ids = (
                candidate.get("operation_query_ids")
                if isinstance(candidate.get("operation_query_ids"), dict)
                else {}
            )
            reachable_query_ids = list(dict.fromkeys(
                str(query_id)
                for operation_id in operation_ids
                for query_id in operation_query_ids.get(operation_id) or []
                if str(query_id)
            ))
            allowed_query_ids = {
                str(query_id)
                for query_id in candidate.get("query_node_ids") or []
                if str(query_id)
            }
            verified_query_ids = [
                query_id
                for query_id in reachable_query_ids
                if query_id in allowed_query_ids
            ]
            metrics.append(PlannedMetric(
                calculation_id=calculation_id,
                output_node_ids=output_ids,
                operation_node_ids=operation_ids,
                query_node_ids=(
                    verified_query_ids
                    or list(candidate.get("query_node_ids") or [])
                ),
                metric_source_fields=metric_fields,
                metric_key=f"structured_{_digest(calculation_id, 12)}",
                business_name=label,
                business_explanation=f"按本轮确认的数据范围和计算口径统计{label}。",
                value_type=value_type,
                display_unit=unit,
            ))
        return EvidencePlan(metrics=metrics)

    @classmethod
    def _unresolved_structured_outputs(
        cls,
        lineage_index: dict[str, Any],
        structured_plan: EvidencePlan,
    ) -> list[dict[str, Any]]:
        planned_ids = {
            str(output_id)
            for metric in structured_plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id)
        }
        return [
            output
            for output in lineage_index.get("output_nodes") or []
            if output.get("artifact_type") in {"chart", "table"}
            and str(output.get("output_node_id") or "") not in planned_ids
            and not cls._is_grouping_dimension_output(output, lineage_index)
        ]

    def _structured_resolver_payload(
        self,
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        referenced_query_ids = {
            str(query_id)
            for output in outputs
            for query_id in output.get("candidate_query_node_ids") or []
            if str(query_id)
        }
        query_catalog = self._planner_query_catalog(
            lineage_index,
            generated_code=snapshot.generated_code,
            query_snapshots=snapshot.query_snapshots,
            selected_datasource_snapshot=snapshot.selected_datasource_snapshot,
        )
        if referenced_query_ids:
            query_catalog = [
                item
                for item in query_catalog
                if str(item.get("query_node_id") or "") in referenced_query_ids
            ]
        return {
            "user_query": snapshot.user_query,
            "analysis_code_with_lines": self._line_numbered_code(
                snapshot.generated_code
            ),
            "structured_output_catalog": [
                {
                    "output_node_id": output.get("output_node_id"),
                    "artifact_type": output.get("artifact_type"),
                    "artifact_index": output.get("artifact_index"),
                    "field": output.get("field"),
                    "label": output.get("label"),
                    "source_frame": output.get("source_frame"),
                    "lineage_fields": output.get("lineage_fields") or [],
                    "display_unit": output.get("display_unit_hint") or "",
                    "candidate_query_node_ids": (
                        output.get("candidate_query_node_ids") or []
                    ),
                }
                for output in outputs
            ],
            "query_catalog": query_catalog,
        }

    def _invoke_structured_planner(self, payload: dict[str, Any]) -> EvidencePlan:
        runtime_config = create_runtime_config()
        model = create_data_insight_model(runtime_config)
        if runtime_config.adapter == "openai":
            model = model.bind(response_format={"type": "json_object"})
        code = str(payload.get("analysis_code_with_lines") or "")
        facts = {
            key: value
            for key, value in payload.items()
            if key != "analysis_code_with_lines"
        }
        messages = [
            SystemMessage(STRUCTURED_EVIDENCE_RESOLVER_PROMPT),
            HumanMessage(_stable_json(facts)),
            HumanMessage("Successful Python code:\n" + code),
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            response_text = ""
            try:
                response = model.invoke(messages)
                content = getattr(response, "content", response)
                response_text = (
                    "".join(
                        str(item.get("text") or "")
                        if isinstance(item, dict)
                        else str(item)
                        for item in content
                    )
                    if isinstance(content, list)
                    else str(content or "")
                )
                plan = EvidencePlan.model_validate(
                    self._canonicalize_planner_payload(
                        _extract_json_object(response_text)
                    )
                )
                allowed_ids = {
                    str(item.get("output_node_id") or "")
                    for item in payload.get("structured_output_catalog") or []
                    if str(item.get("output_node_id") or "")
                }
                claims = [
                    str(output_id)
                    for metric in plan.metrics
                    for output_id in metric.output_node_ids
                    if str(output_id)
                ] + [
                    str(item.output_node_id)
                    for item in plan.unresolved_outputs
                    if str(item.output_node_id)
                ]
                unknown = set(claims) - allowed_ids
                duplicate = {item for item in claims if claims.count(item) > 1}
                missing = allowed_ids - set(claims)
                invalid_metrics = [
                    index
                    for index, metric in enumerate(plan.metrics)
                    if not metric.output_node_ids
                    or not metric.calculation_source_refs
                    or not metric.claimed_query_node_ids
                    or not metric.claimed_metric_fields
                ]
                if unknown or duplicate or missing or invalid_metrics:
                    raise ValueError(
                        "invalid structured resolver coverage: "
                        f"unknown={sorted(unknown)} duplicate={sorted(duplicate)} "
                        f"missing={sorted(missing)} invalid_metrics={invalid_metrics}"
                    )
                return plan
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 0:
                    if response_text:
                        messages.append(AIMessage(response_text))
                    messages.append(HumanMessage(
                        "Repair only the JSON contract and catalog coverage. "
                        + str(exc)[:1000]
                    ))
                    continue
                raise
        raise RuntimeError(f"Structured Evidence resolver failed: {last_error}")

    def _materialize_structured_recovery_plan(
        self,
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        plan: EvidencePlan,
        outputs: list[dict[str, Any]],
    ) -> tuple[EvidencePlan, dict[str, dict[str, Any]]]:
        allowed_outputs = {
            str(output.get("output_node_id") or ""): output
            for output in outputs
            if str(output.get("output_node_id") or "")
        }
        successful_query_ids = {
            str(query.get("query_node_id") or "")
            for query in lineage_index.get("query_nodes") or []
            if str(query.get("query_node_id") or "")
        }
        resolved: list[PlannedMetric] = []
        unresolved = list(plan.unresolved_outputs)
        candidates: dict[str, dict[str, Any]] = {}
        claimed_outputs: set[str] = {
            str(item.output_node_id)
            for item in unresolved
            if str(item.output_node_id)
        }

        for metric_index, metric in enumerate(plan.metrics):
            output_ids = [
                str(output_id)
                for output_id in metric.output_node_ids
                if str(output_id) in allowed_outputs
                and str(output_id) not in claimed_outputs
            ]
            if not output_ids:
                continue
            operation_by_id = {
                str(item.get("operation_node_id") or ""): item
                for item in lineage_index.get("operation_nodes") or []
                if str(item.get("operation_node_id") or "")
            }
            recovered, errors = self._resolve_calculation_source_refs(
                snapshot.generated_code,
                lineage_index,
                metric.calculation_source_refs,
            )
            operation_by_id = {
                str(item.get("operation_node_id") or ""): item
                for item in lineage_index.get("operation_nodes") or []
                if str(item.get("operation_node_id") or "")
            }
            producers: dict[str, list[str]] = {}
            for operation_id, operation in operation_by_id.items():
                for target in operation.get("targets") or []:
                    target_name = str(target or "").strip()
                    if target_name:
                        producers.setdefault(target_name, []).append(operation_id)
            operation_ids = list(dict.fromkeys([
                *[
                    str(item)
                    for item in metric.operation_node_ids
                    if str(item) in operation_by_id
                ],
                *recovered,
            ]))
            derived_query_ids = list(dict.fromkeys(
                query_id
                for operation_id in operation_ids
                for query_id in LineageIndexer._query_dependencies(
                    [operation_id], operation_by_id, producers
                )
                if query_id in successful_query_ids
            ))
            claimed_query_ids = list(dict.fromkeys(
                str(item)
                for item in metric.claimed_query_node_ids
                if str(item) in successful_query_ids
            ))
            local_output_fields = list(dict.fromkeys(
                str(item).strip()
                for item in [
                    field
                    for output_id in output_ids
                    for field in (
                        allowed_outputs[output_id].get("lineage_fields")
                        or [allowed_outputs[output_id].get("field")]
                    )
                ]
                if str(item).strip()
            ))
            query_ids = derived_query_ids
            verification = "verified"
            claimed_fields = list(dict.fromkeys(
                str(field).strip()
                for field in metric.claimed_metric_fields
                if str(field).strip()
            ))
            claimed_known_fields = {
                field
                for query_id in claimed_query_ids
                for field in self._query_reachable_fields(
                    query_id,
                    snapshot.query_snapshots.get(query_id),
                    operation_by_id,
                    producers,
                )
            }
            derived_known_fields = {
                field
                for query_id in derived_query_ids
                for field in self._query_reachable_fields(
                    query_id,
                    snapshot.query_snapshots.get(query_id),
                    operation_by_id,
                    producers,
                )
            }
            missing_claimed_query_edges = set(claimed_query_ids) - set(derived_query_ids)
            plausible_missing_query_claim = bool(
                missing_claimed_query_edges
                and claimed_fields
                and set(claimed_fields).issubset(claimed_known_fields)
                and set(claimed_fields).intersection(derived_known_fields)
            )
            can_attest_missing_query_edges = bool(
                not errors
                and operation_ids
                and derived_query_ids
                and plausible_missing_query_claim
                and metric.attestation_confidence == "high"
            )
            if can_attest_missing_query_edges:
                attested_query_ids = list(dict.fromkeys([
                    *derived_query_ids,
                    *claimed_query_ids,
                ]))
                if set(attested_query_ids) != set(derived_query_ids):
                    query_ids = attested_query_ids
                    verification = "attested"
            elif plausible_missing_query_claim:
                errors.append(
                    "additional query dependency is not locally proven with "
                    "high-confidence attestation"
                )
            known_fields = {
                field
                for query_id in query_ids
                for field in self._query_reachable_fields(
                    query_id,
                    snapshot.query_snapshots.get(query_id),
                    operation_by_id,
                    producers,
                )
            }
            if verification == "verified":
                # Structured artifact lineage is the authoritative contract.
                # Planner claims are advisory and must never replace or veto a
                # locally proven output -> operation -> query closure.
                metric_fields = [
                    field for field in local_output_fields if field in known_fields
                ]
                if not metric_fields:
                    metric_fields = [
                        str(field).strip()
                        for field in metric.claimed_metric_fields
                        if str(field).strip() in known_fields
                    ]
            else:
                metric_fields = claimed_fields
            unknown_fields = set(metric_fields) - known_fields
            if (
                errors
                or not operation_ids
                or not query_ids
                or not metric_fields
                or unknown_fields
            ):
                reason = "; ".join([
                    *errors,
                    *( [f"unknown query fields: {sorted(unknown_fields)}"] if unknown_fields else [] ),
                    *( ["no calculation operation"] if not operation_ids else [] ),
                    *( ["no verified query"] if not query_ids else [] ),
                ])
                unresolved.extend(
                    UnresolvedEvidenceOutput(
                        output_node_id=output_id,
                        reason=reason or "Structured calculation boundary is not provable.",
                    )
                    for output_id in output_ids
                )
                claimed_outputs.update(output_ids)
                continue
            calculation_id = (
                f"structured_recovery_{metric_index}_"
                f"{_digest(_stable_json(output_ids), 10)}"
            )
            recovered_metric = metric.model_copy(update={
                "calculation_id": calculation_id,
                "output_node_ids": output_ids,
                "operation_node_ids": operation_ids,
                "query_node_ids": query_ids,
                "metric_source_fields": metric_fields,
                "lineage_verification": verification,
                "display_unit": (
                    metric.display_unit
                    or str(allowed_outputs[output_ids[0]].get("display_unit_hint") or "")
                ),
            })
            resolved.append(recovered_metric)
            candidates[calculation_id] = {
                "calculation_id": calculation_id,
                "output_node_ids": output_ids,
                "operation_node_ids": operation_ids,
                "boundary_operation_node_ids": operation_ids,
                "query_node_ids": query_ids,
                "metric_source_fields": metric_fields,
                "recovery_candidate": True,
                "semantic_scope": False,
                "ambiguous_input": False,
                "parameter_bindings": metric.parameter_bindings,
            }
            claimed_outputs.update(output_ids)

        unresolved.extend(
            UnresolvedEvidenceOutput(
                output_node_id=output_id,
                reason="Structured resolver did not classify this output.",
            )
            for output_id in allowed_outputs
            if output_id not in claimed_outputs
        )
        return EvidencePlan(
            metrics=resolved,
            unresolved_outputs=self._normalize_unresolved_report_outputs(
                EvidencePlan(unresolved_outputs=unresolved)
            ).unresolved_outputs,
        ), candidates

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
                "recovery_candidate": bool(candidate.get("recovery_candidate")),
                "semantic_scope": bool(candidate.get("semantic_scope")),
                "local_lineage_hints": candidate.get("local_lineage_hints") or {},
                "output_operation_ids": candidate.get("output_operation_ids") or {},
                "ambiguity_requires_resolution": bool(
                    candidate.get("ambiguous_input")
                ),
                "parameter_bindings": candidate.get("parameter_bindings") or {},
                "metric_source_fields": candidate.get("metric_source_fields") or [],
                "available_metric_fields": candidate.get("available_metric_fields") or [],
                "output_node_ids": [
                    str(output_id) for output_id in candidate.get("output_node_ids") or []
                    if str(output_id)
                ],
                "operation_node_ids": [
                    str(operation_id)
                    for operation_id in candidate.get("boundary_operation_node_ids") or []
                    if str(operation_id)
                ],
                "query_node_ids": [
                    str(query_id) for query_id in candidate.get("query_node_ids") or []
                    if str(query_id)
                ],
                "operation_query_ids": candidate.get("operation_query_ids") or {},
                "operation_family_signatures": (
                    candidate.get("operation_family_signatures") or {}
                ),
                "operation_write_signatures": (
                    candidate.get("operation_write_signatures") or {}
                ),
                "operation_semantic_kinds": (
                    candidate.get("operation_semantic_kinds") or {}
                ),
                "operations": [
                    {
                        "operation_node_id": str(operation.get("operation_node_id") or ""),
                        "kind": operation.get("semantic_kind") or operation.get("kind") or "",
                        "expression": str(operation.get("source") or "").strip(),
                    }
                    for operation in boundary_operations
                    if str(operation.get("operation_node_id") or "")
                ],
                "calculation_expressions": list(dict.fromkeys(
                    str(operation.get("source") or "").strip()
                    for operation in boundary_operations
                    if str(operation.get("source") or "").strip()
                )),
                "outputs": [
                    {
                        "output_node_id": str(output.get("output_node_id") or ""),
                        "source_anchor_id": str(output.get("source_anchor_id") or ""),
                        "artifact_type": output.get("artifact_type"),
                        "artifact_index": output.get("artifact_index"),
                        "label": output.get("label"),
                        "field": output.get("field"),
                        "lineage_fields": output.get("lineage_fields") or [],
                        "sample_values": output.get("sample_values") or [],
                        "display_unit_hint": output.get("display_unit_hint") or "",
                        "source_line": output.get("source_line"),
                        "display_context": {
                            "prefix": output.get("display_prefix") or "",
                            "suffix": output.get("display_suffix") or "",
                        } if output.get("artifact_type") == "report" else {},
                    }
                    for output_id in candidate.get("output_node_ids") or []
                    for output in [output_by_id.get(str(output_id)) or {}]
                ],
            })
        return payloads

    @staticmethod
    def _planner_query_catalog(
        lineage_index: dict[str, Any],
        *,
        generated_code: str = "",
        query_snapshots: dict[str, Any] | None = None,
        selected_datasource_snapshot: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Expose executed query semantics without duplicating SQL already present in code."""
        result_bindings: dict[str, list[dict[str, Any]]] = {}
        if generated_code.strip() and query_snapshots:
            try:
                tree = ast.parse(generated_code)
                expression_bindings = LineageIndexer._query_expression_bindings(
                    tree,
                    query_snapshots,
                )
                for node in sorted(
                    (
                        item
                        for item in ast.walk(tree)
                        if isinstance(item, (ast.Assign, ast.AnnAssign))
                    ),
                    key=lambda item: int(getattr(item, "lineno", 0) or 0),
                ):
                    query_node_id = LineageIndexer._load_query_node_id(
                        node,
                        expression_bindings,
                    )
                    if not query_node_id:
                        continue
                    for target in _target_names(node):
                        if "[" in target or "." in target:
                            continue
                        result_bindings.setdefault(query_node_id, []).append({
                            "variable": target,
                            "source_line": int(getattr(node, "lineno", 0) or 0),
                        })
            except SyntaxError:
                result_bindings = {}
        catalog: list[dict[str, Any]] = []
        for item in lineage_index.get("query_nodes") or []:
            query_node_id = str(item.get("query_node_id") or "")
            if not query_node_id:
                continue
            scope = item.get("query_scope") if isinstance(item.get("query_scope"), dict) else {}
            implementation = (
                scope.get("implementation")
                if isinstance(scope.get("implementation"), dict)
                else {}
            )
            implementation_text = _stable_json(implementation).casefold()
            datasource_matches = [
                datasource
                for datasource in selected_datasource_snapshot or []
                if isinstance(datasource, dict)
                and str(datasource.get("datasource_identifier") or "").strip()
                and str(datasource.get("datasource_identifier") or "")
                .strip()
                .casefold() in implementation_text
            ]
            datasource_identity = (
                {
                    "datasource_id": (
                        datasource_matches[0].get("datasource_id")
                        or datasource_matches[0].get("id")
                    ),
                    "datasource_name": (
                        datasource_matches[0].get("datasource_name")
                        or datasource_matches[0].get("display_name")
                        or datasource_matches[0].get("displayName")
                        or datasource_matches[0].get("name")
                        or ""
                    ),
                }
                if len(datasource_matches) == 1
                else {}
            )
            catalog.append({
                "query_node_id": query_node_id,
                **datasource_identity,
                "columns": list(dict.fromkeys(
                    str(field)
                    for field in scope.get("execution_columns") or []
                    if str(field)
                )),
                "query_constraints": scope.get("query_constraints") or {},
                "aggregation_hints": scope.get("aggregation_hints") or [],
                "result_bindings": result_bindings.get(query_node_id, []),
                "data_access": {
                    key: implementation.get(key)
                    for key in ("datasource_identifier", "loader")
                    if implementation.get(key) not in (None, "")
                },
                "projection_semantics": [
                    {
                        key: projection.get(key)
                        for key in (
                            "alias", "semantic_kind", "projection_role", "source_fields",
                        )
                        if projection.get(key) not in (None, "", [])
                    }
                    for projection in implementation.get("projection_semantics") or []
                    if isinstance(projection, dict)
                ],
            })
        return catalog

    @staticmethod
    def _relevant_datasource_context(
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        *,
        search_text: str = "",
    ) -> list[dict[str, Any]]:
        search_text = search_text or "\n".join(
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

    @staticmethod
    def _report_candidate_catalog(
        lineage_index: dict[str, Any],
        *,
        excluded_output_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Group rendered report candidates by their Evidence binding boundary.

        Prose values remain single-span candidates. Markdown table values are
        grouped by table column so the semantic planner classifies one metric
        boundary rather than every cell in a repeated data grid.
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        excluded_output_ids = excluded_output_ids or set()
        for output in lineage_index.get("output_nodes") or []:
            if output.get("artifact_type") != "report" or not (
                output.get("posthoc_report_candidate")
                or output.get("ambiguous_calculation_boundary")
            ):
                continue
            if not PostAnalysisEvidenceCompiler._is_report_metric_output(
                output,
                lineage_index,
            ):
                continue
            output_id = str(output.get("output_node_id") or "")
            if not output_id or output_id in excluded_output_ids:
                continue
            # One dynamic template expression can render several independent
            # metrics through a loop variable. Keep those occurrences separate
            # so semantic resolution cannot collapse their Evidence boundary.
            group_id = str(
                output.get("report_candidate_group_id")
                if output.get("posthoc_report_candidate")
                else output_id
            )
            groups.setdefault(group_id, []).append(output)
        catalog: list[dict[str, Any]] = []
        for group_id, outputs in groups.items():
            representative = outputs[0]
            context = representative.get("display_context") or {}
            catalog.append({
                "candidate_id": group_id,
                "text_examples": list(dict.fromkeys(
                    str(output.get("text") or "") for output in outputs
                    if str(output.get("text") or "")
                ))[:3],
                "heading": str(context.get("heading") or representative.get("label") or ""),
                "column_label": str(context.get("column_label") or representative.get("label") or ""),
                "line": str(context.get("line") or ""),
                "token_prefix": str(context.get("token_prefix") or ""),
                "token_suffix": str(context.get("token_suffix") or ""),
                "rendered_context": str(
                    representative.get("rendered_context")
                    or context.get("line")
                    or ""
                ),
                "slot_bindings": representative.get("rendered_slot_bindings") or {},
                "occurrence_count": len(outputs),
                "kind": (
                    "ambiguous_template_occurrence"
                    if representative.get("ambiguous_calculation_boundary")
                    else "markdown_table_column"
                    if len(outputs) > 1
                    else "report_span"
                ),
                "runtime_iteration_boundary_required": bool(
                    representative.get("runtime_iteration_boundary_required")
                ),
                "candidate_query_node_ids": list(dict.fromkeys(
                    str(query_id)
                    for output in outputs
                    for query_id in output.get("candidate_query_node_ids") or []
                    if str(query_id)
                )),
            })
        return catalog

    @staticmethod
    def _attach_candidate_source_line_hints(
        catalog: list[dict[str, Any]],
        code: str,
        source_anchors: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Locate report-construction lines by shared static text only."""
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return catalog

        def normalized(value: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").lower())

        static_fragments: list[tuple[int, str, str]] = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            ):
                continue
            text = str(node.value or "").strip()
            compact = normalized(text)
            if len(compact) < 4:
                continue
            static_fragments.append((int(getattr(node, "lineno", 0) or 0), text, compact))

        enriched: list[dict[str, Any]] = []
        for candidate in catalog:
            candidate_text = normalized(" ".join([
                str(candidate.get("heading") or ""),
                str(candidate.get("column_label") or ""),
                str(candidate.get("line") or ""),
            ]))
            matches = sorted(
                (
                    (len(compact), line, text)
                    for line, text, compact in static_fragments
                    if line > 0 and compact in candidate_text
                ),
                reverse=True,
            )
            hints: list[dict[str, Any]] = []
            token_prefix = str(candidate.get("token_prefix") or "")
            token_suffix = str(candidate.get("token_suffix") or "")
            text_examples = [
                str(item or "")
                for item in candidate.get("text_examples") or []
            ]
            for anchor in source_anchors or []:
                display_prefix = str(anchor.get("display_prefix") or "")
                display_suffix = str(anchor.get("display_suffix") or "")
                numeric_suffix = str(anchor.get("numeric_suffix") or "")
                # A slot without any surrounding presentation marker matches
                # every rendered number and therefore carries no location
                # evidence. Exposing it as a hint leaks unrelated internal
                # anchors into the semantic resolver's bounded catalog.
                if not (display_prefix or display_suffix or numeric_suffix):
                    continue
                prefix_matches = (
                    not display_prefix or token_prefix.endswith(display_prefix)
                )
                suffix_matches = (
                    not display_suffix or token_suffix.startswith(display_suffix)
                )
                numeric_suffix_matches = (
                    not numeric_suffix
                    or any(
                        token_suffix.startswith(suffix)
                        for suffix in {numeric_suffix, f"{numeric_suffix} "}
                    )
                )
                if not (prefix_matches and suffix_matches and numeric_suffix_matches):
                    continue
                line = int(anchor.get("source_line") or 0)
                expression = str(anchor.get("expression") or "").strip()
                if line <= 0 or not expression:
                    continue
                hints.append({
                    "line": line,
                    "expression": expression,
                    "anchor_id": str(anchor.get("anchor_id") or ""),
                    "display_prefix": display_prefix,
                    "display_suffix": display_suffix,
                    "numeric_suffix": numeric_suffix,
                    "match_kind": "rendered_slot",
                    "text_examples": text_examples,
                })
            seen_lines: set[int] = set()
            for _, line, text in matches:
                if line in seen_lines:
                    continue
                seen_lines.add(line)
                hints.append({
                    "line": line,
                    "matched_text": text[:160],
                    "match_kind": "static_text",
                })
                if len(hints) >= 6:
                    break
            enriched.append({
                **candidate,
                "source_line_hints": sorted(hints, key=lambda item: int(item["line"])),
                "nearby_source_range": (
                    [
                        max(1, min(int(item["line"]) for item in hints) - 4),
                        max(int(item["line"]) for item in hints) + 4,
                    ]
                    if hints
                    else []
                ),
            })
        return enriched

    def _planner_fact_payload(
        self,
        snapshot: AnalysisExecutionSnapshot,
        lineage_index: dict[str, Any],
        calculation_candidates: dict[str, dict[str, Any]],
        *,
        recovery_only: bool = False,
        excluded_output_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build one normalized fact graph without repeating operations or outputs."""
        expanded = self._planner_candidate_payloads(calculation_candidates, lineage_index)
        operation_catalog: dict[str, dict[str, Any]] = {}
        output_catalog: dict[str, dict[str, Any]] = {}
        candidates: list[dict[str, Any]] = []
        for candidate in expanded:
            for operation in candidate.get("operations") or []:
                operation_id = str(operation.get("operation_node_id") or "")
                if operation_id:
                    operation_catalog[operation_id] = operation
            for output in candidate.get("outputs") or []:
                output_id = str(output.get("output_node_id") or "")
                if output_id:
                    output_catalog[output_id] = output
            candidates.append({
                "calculation_id": candidate.get("calculation_id"),
                "calculation_kind": candidate.get("calculation_kind"),
                "recovery_candidate": bool(candidate.get("recovery_candidate")),
                "semantic_scope": bool(candidate.get("semantic_scope")),
                "evidence_required": bool(
                    not candidate.get("semantic_scope")
                    and candidate.get("operation_node_ids")
                    and candidate.get("query_node_ids")
                    and candidate.get("available_metric_fields")
                ),
                "ambiguity_requires_resolution": bool(
                    candidate.get("ambiguity_requires_resolution")
                ),
                "parameter_bindings": candidate.get("parameter_bindings") or {},
                "metric_source_fields": candidate.get("metric_source_fields") or [],
                "available_metric_fields": candidate.get("available_metric_fields") or [],
                "output_node_ids": candidate.get("output_node_ids") or [],
                "operation_node_ids": candidate.get("operation_node_ids") or [],
                "query_node_ids": candidate.get("query_node_ids") or [],
                "operation_query_ids": candidate.get("operation_query_ids") or {},
                "operation_family_signatures": (
                    candidate.get("operation_family_signatures") or {}
                ),
                "operation_write_signatures": (
                    candidate.get("operation_write_signatures") or {}
                ),
                "operation_semantic_kinds": (
                    candidate.get("operation_semantic_kinds") or {}
                ),
                "local_lineage_hints": candidate.get("local_lineage_hints") or {},
                "output_operation_ids": candidate.get("output_operation_ids") or {},
            })
        referenced_query_ids = {
            str(query_id)
            for candidate in candidates
            for query_id in candidate.get("query_node_ids") or []
            if str(query_id)
        }
        report_outputs_by_anchor: dict[str, list[dict[str, Any]]] = {}
        for output in lineage_index.get("output_nodes") or []:
            if output.get("artifact_type") != "report":
                continue
            anchor_id = str(output.get("source_anchor_id") or "")
            if anchor_id:
                report_outputs_by_anchor.setdefault(anchor_id, []).append(output)

        locally_verified_report_output_ids = {
            str(output_id)
            for candidate in calculation_candidates.values()
            for output_id, hint in (candidate.get("local_lineage_hints") or {}).items()
            if str(output_id)
            and not bool((hint or {}).get("ambiguous"))
        }
        # The compile pass may already have proven report slots from the
        # calculation graph. Their source anchors are not recovery work.
        locally_verified_report_output_ids.update(excluded_output_ids or set())
        # The planner is a semantic recovery step, not a second classifier for
        # every already-proven report slot. Keep only rendered numeric outputs
        # that the local lineage graph could not bind to a calculation boundary.
        discovery_report_anchor_ids = {
            str(output.get("source_anchor_id") or "")
            for output in lineage_index.get("output_nodes") or []
            if output.get("artifact_type") == "report"
            and self._is_report_metric_output(output, lineage_index)
            and str(output.get("output_node_id") or "")
            not in locally_verified_report_output_ids
            and str(output.get("source_anchor_id") or "")
        }
        report_candidate_catalog = self._attach_candidate_source_line_hints(
            self._report_candidate_catalog(
                lineage_index,
                excluded_output_ids=locally_verified_report_output_ids,
            ),
            snapshot.generated_code,
            lineage_index.get("report_source_anchors") or [],
        )
        occurrence_candidate_anchor_ids = {
            str(output.get("source_anchor_id") or "")
            for output in lineage_index.get("output_nodes") or []
            if output.get("artifact_type") == "report"
            and output.get("ambiguous_calculation_boundary")
            and str(output.get("output_node_id") or "")
            not in locally_verified_report_output_ids
            and str(output.get("source_anchor_id") or "")
        }
        source_anchor_catalog = [
            {
                **{
                    key: value
                    for key, value in anchor.items()
                    if key in {
                        "anchor_id", "source_line", "expression", "loads",
                        "display_prefix", "display_suffix", "numeric_hint",
                        "numeric_suffix",
                    }
                },
                "verified_report_output": bool(
                    report_outputs_by_anchor.get(str(anchor.get("anchor_id") or ""))
                ),
                "query_reachable": any(
                    output.get("candidate_query_node_ids")
                    for output in report_outputs_by_anchor.get(
                        str(anchor.get("anchor_id") or ""), []
                    )
                ),
                "dimension_origin": bool(
                    report_outputs_by_anchor.get(str(anchor.get("anchor_id") or ""))
                ) and all(
                    self._is_grouping_dimension_output(output, lineage_index)
                    for output in report_outputs_by_anchor.get(
                        str(anchor.get("anchor_id") or ""), []
                    )
                ),
            }
            for anchor in lineage_index.get("report_source_anchors") or []
            if (
                str(anchor.get("anchor_id") or "")
                not in occurrence_candidate_anchor_ids
                and (
                    not recovery_only
                    or str(anchor.get("anchor_id") or "")
                    in discovery_report_anchor_ids
                )
            )
        ]
        discovery_search_text = _stable_json({
            "user_query": snapshot.user_query,
            "report_candidates": report_candidate_catalog,
            "source_anchors": source_anchor_catalog,
        })
        relevant_datasources = self._relevant_datasource_context(
            snapshot,
            lineage_index,
            search_text=(discovery_search_text if recovery_only else ""),
        )
        query_catalog = [
            item for item in self._planner_query_catalog(
                lineage_index,
                generated_code=snapshot.generated_code,
                query_snapshots=snapshot.query_snapshots,
                selected_datasource_snapshot=snapshot.selected_datasource_snapshot,
            )
            if str(item.get("query_node_id") or "") in referenced_query_ids
        ]
        if recovery_only:
            query_catalog = [
                {
                    "query_node_id": item.get("query_node_id"),
                    "datasource_id": item.get("datasource_id"),
                    "datasource_name": item.get("datasource_name") or "",
                    "data_access": item.get("data_access") or {},
                    "columns": item.get("columns") or [],
                    "result_bindings": item.get("result_bindings") or [],
                    "projection_semantics": [
                        projection
                        for projection in item.get("projection_semantics") or []
                        if any(
                            str(value) and str(value) in discovery_search_text
                            for value in (
                                [projection.get("alias")]
                                + (
                                    list(projection.get("source_fields") or [])
                                    if isinstance(projection.get("source_fields"), list)
                                    else [projection.get("source_fields")]
                                )
                            )
                        )
                    ],
                }
                for item in query_catalog
            ]
        recovery_branch_operations = [
            {
                "operation_node_id": str(
                    operation.get("operation_node_id") or ""
                ),
                "kind": operation.get("semantic_kind") or "calculation",
                "expression": str(operation.get("source") or "").strip(),
                "parameter_bindings": dict(
                    operation.get("parameter_bindings") or {}
                ),
            }
            for operation in lineage_index.get("operation_nodes") or []
            if recovery_only
            and str(operation.get("operation_node_id") or "").startswith(
                "op_mapping_member_"
            )
            and str(operation.get("source") or "").strip()
        ][:80]
        payload = {
            "user_query": snapshot.user_query,
            "analysis_code_with_lines": self._line_numbered_code(snapshot.generated_code),
            "report_candidate_catalog": report_candidate_catalog,
            "source_anchor_catalog": source_anchor_catalog,
            "datasource_catalog": self._planner_datasource_catalog(
                relevant_datasources
            ),
            "query_catalog": query_catalog,
            "operation_catalog": (
                recovery_branch_operations
                if recovery_only
                else list(operation_catalog.values())
            ),
            "calculation_selection_mode": (
                "source_locator" if recovery_only else "operation_or_source_locator"
            ),
            "output_catalog": list(output_catalog.values()),
            "calculation_candidates": candidates,
        }
        return self._discovery_planner_payload(self._alias_planner_fact_payload(payload))

    @staticmethod
    def _discovery_planner_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Remove local report answers while retaining independently verifiable facts."""
        hints_by_anchor: dict[str, list[dict[str, Any]]] = {}
        outputs = {
            str(item.get("output_node_id") or ""): item
            for item in payload.get("output_catalog") or []
        }
        for candidate in payload.get("calculation_candidates") or []:
            if not candidate.get("semantic_scope"):
                continue
            local_hints = candidate.get("local_lineage_hints") or {}
            for output_id, hint in local_hints.items():
                output = outputs.get(str(output_id)) or {}
                for anchor_id in output.get("source_anchor_ids") or []:
                    hints_by_anchor.setdefault(str(anchor_id), []).append({
                        "operation_node_ids": list(hint.get("operation_node_ids") or []),
                        "parameter_bindings": dict(hint.get("parameter_bindings") or {}),
                        "lineage_fields": list(hint.get("output_lineage_fields") or []),
                    })
        return {
            key: value
            for key, value in {
                **payload,
                "source_anchor_catalog": [
                    {
                        **anchor,
                        "verification_hints": list({
                            _stable_json(hint): hint
                            for hint in hints_by_anchor.get(
                                str(anchor.get("anchor_id") or ""), []
                            )
                        }.values()),
                    }
                    for anchor in payload.get("source_anchor_catalog") or []
                ],
            }.items()
            if key not in {"output_catalog", "calculation_candidates"}
        }

    @staticmethod
    def _line_numbered_code(code: str) -> str:
        return "\n".join(
            f"{index:04d}: {line}"
            for index, line in enumerate(str(code or "").splitlines(), start=1)
        )

    def _alias_planner_fact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        def build_aliases(values: list[str], prefix: str) -> tuple[dict[str, str], dict[str, str]]:
            forward = {
                value: f"{prefix}{index}"
                for index, value in enumerate(dict.fromkeys(values), start=1)
                if value
            }
            return forward, {alias: value for value, alias in forward.items()}

        query_forward, query_reverse = build_aliases([
            str(item.get("query_node_id") or "")
            for item in payload.get("query_catalog") or []
        ], "q")
        operation_forward, operation_reverse = build_aliases([
            str(item.get("operation_node_id") or "")
            for item in payload.get("operation_catalog") or []
        ], "op")
        calculation_forward, calculation_reverse = build_aliases([
            str(item.get("calculation_id") or "")
            for item in payload.get("calculation_candidates") or []
        ], "calc")
        anchor_forward, anchor_reverse = build_aliases([
            str(item.get("anchor_id") or "")
            for item in payload.get("source_anchor_catalog") or []
        ], "a")
        report_candidate_forward, report_candidate_reverse = build_aliases([
            str(item.get("candidate_id") or "")
            for item in payload.get("report_candidate_catalog") or []
        ], "rc")
        calculation_by_output = {
            str(output_id): str(candidate.get("calculation_id") or "")
            for candidate in payload.get("calculation_candidates") or []
            for output_id in candidate.get("output_node_ids") or []
        }
        local_hint_by_output = {
            str(output_id): hint
            for candidate in payload.get("calculation_candidates") or []
            for output_id, hint in (candidate.get("local_lineage_hints") or {}).items()
        }
        output_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for output in payload.get("output_catalog") or []:
            output_id = str(output.get("output_node_id") or "")
            key = (
                calculation_by_output.get(output_id, ""),
                str(output.get("field") or ""),
                str(output.get("label") or ""),
                str(output.get("display_unit_hint") or ""),
                _stable_json(local_hint_by_output.get(output_id) or {}),
            )
            output_groups.setdefault(key, []).append(output)
        output_forward: dict[str, str] = {}
        output_reverse: dict[str, list[str]] = {}
        aliased_output_catalog: list[dict[str, Any]] = []
        for index, outputs in enumerate(output_groups.values(), start=1):
            alias = f"out{index}"
            internal_ids = [
                str(output.get("output_node_id") or "") for output in outputs
                if str(output.get("output_node_id") or "")
            ]
            for output_id in internal_ids:
                output_forward[output_id] = alias
            output_reverse[alias] = internal_ids
            representative = dict(outputs[0])
            contexts: list[dict[str, Any]] = []
            for output in outputs:
                context = output.get("display_context")
                if isinstance(context, dict) and context not in contexts:
                    contexts.append(context)
            representative["output_node_id"] = alias
            source_anchor_ids = list(dict.fromkeys(
                anchor_forward.get(source_anchor_id, source_anchor_id)
                for output in outputs
                for source_anchor_id in [str(output.get("source_anchor_id") or "")]
                if source_anchor_id
            ))
            representative["source_anchor_ids"] = source_anchor_ids
            representative.pop("source_anchor_id", None)
            representative["occurrence_count"] = len(outputs)
            representative["display_context_examples"] = contexts[:3]
            representative.pop("display_context", None)
            aliased_output_catalog.append(representative)
        self._resolver_id_maps = {
            "query": query_reverse,
            "operation": operation_reverse,
            "output": output_reverse,
            "calculation": calculation_reverse,
            "anchor": anchor_reverse,
            "report_candidate": report_candidate_reverse,
        }
        return {
            **payload,
            "report_candidate_catalog": [
                {
                    **item,
                    "candidate_id": report_candidate_forward.get(
                        str(item.get("candidate_id") or ""),
                        str(item.get("candidate_id") or ""),
                    ),
                }
                for item in payload.get("report_candidate_catalog") or []
            ],
            "source_anchor_catalog": [
                {
                    **item,
                    "anchor_id": anchor_forward.get(
                        str(item.get("anchor_id") or ""),
                        str(item.get("anchor_id") or ""),
                    ),
                }
                for item in payload.get("source_anchor_catalog") or []
            ],
            "query_catalog": [
                {**item, "query_node_id": query_forward.get(str(item.get("query_node_id") or ""), "")}
                for item in payload.get("query_catalog") or []
            ],
            "operation_catalog": [
                {
                    **item,
                    "operation_node_id": operation_forward.get(
                        str(item.get("operation_node_id") or ""), ""
                    ),
                }
                for item in payload.get("operation_catalog") or []
            ],
            "output_catalog": aliased_output_catalog,
            "calculation_candidates": [
                {
                    **item,
                    "calculation_id": calculation_forward.get(str(item.get("calculation_id") or ""), ""),
                    "output_node_ids": [
                        value for value in dict.fromkeys(
                            output_forward.get(str(output_id), "")
                            for output_id in item.get("output_node_ids") or []
                        ) if value
                    ],
                    "operation_node_ids": [
                        operation_forward.get(str(value), "")
                        for value in item.get("operation_node_ids") or []
                    ],
                    "query_node_ids": [
                        query_forward.get(str(value), "") for value in item.get("query_node_ids") or []
                    ],
                    "operation_query_ids": {
                        operation_forward.get(str(operation_id), ""): [
                            query_forward.get(str(query_id), "")
                            for query_id in query_ids
                            if query_forward.get(str(query_id), "")
                        ]
                        for operation_id, query_ids in (
                            item.get("operation_query_ids") or {}
                        ).items()
                        if operation_forward.get(str(operation_id), "")
                    },
                    "operation_family_signatures": {
                        operation_forward.get(str(operation_id), ""): signature
                        for operation_id, signature in (
                            item.get("operation_family_signatures") or {}
                        ).items()
                        if operation_forward.get(str(operation_id), "")
                    },
                    "operation_write_signatures": {
                        operation_forward.get(str(operation_id), ""): signature
                        for operation_id, signature in (
                            item.get("operation_write_signatures") or {}
                        ).items()
                        if operation_forward.get(str(operation_id), "")
                    },
                    "operation_semantic_kinds": {
                        operation_forward.get(str(operation_id), ""): semantic_kind
                        for operation_id, semantic_kind in (
                            item.get("operation_semantic_kinds") or {}
                        ).items()
                        if operation_forward.get(str(operation_id), "")
                    },
                    "local_lineage_hints": {
                        output_forward.get(str(output_id), ""): {
                            **hint,
                            "local_boundary_key": str(hint.get("calculation_id") or ""),
                            "calculation_id": "",
                            "operation_node_ids": [
                                operation_forward.get(str(operation_id), "")
                                for operation_id in hint.get("operation_node_ids") or []
                                if operation_forward.get(str(operation_id), "")
                            ],
                            "query_node_ids": [
                                query_forward.get(str(query_id), "")
                                for query_id in hint.get("query_node_ids") or []
                                if query_forward.get(str(query_id), "")
                            ],
                        }
                        for output_id, hint in (item.get("local_lineage_hints") or {}).items()
                        if output_forward.get(str(output_id), "")
                    },
                    "output_operation_ids": {
                        output_forward.get(str(output_id), ""): [
                            operation_forward.get(str(operation_id), "")
                            for operation_id in operation_ids
                            if operation_forward.get(str(operation_id), "")
                        ]
                        for output_id, operation_ids in (
                            item.get("output_operation_ids") or {}
                        ).items()
                        if output_forward.get(str(output_id), "")
                    },
                }
                for item in payload.get("calculation_candidates") or []
            ],
        }

    def _normalize_typed_resolver_aliases(self, plan: EvidencePlan) -> EvidencePlan:
        """Repair only cross-field alias placement before alias validation.

        The planner receives typed short IDs: ``a*`` for source expressions and
        ``rc*`` for rendered report candidates. It can occasionally put a valid
        ID in the other field. Keep aliases intact for the following validation
        pass, while moving them to their declared type.
        """
        maps = self._resolver_id_maps
        if not maps:
            return plan
        candidate_aliases = set(maps.get("report_candidate", {}))
        anchor_aliases = set(maps.get("anchor", {}))

        def normalize_metric(metric: PlannedMetric) -> PlannedMetric:
            anchors: list[ReportSourceAnchorRef] = []
            candidates: list[str] = []
            for anchor in metric.source_anchors:
                alias = str(anchor.anchor_id or "")
                if alias in candidate_aliases:
                    candidates.append(alias)
                elif not anchor_aliases and metric.report_candidate_ids:
                    # Candidate-only discovery has no source-anchor catalog.
                    # A model may still emit placeholder ``a*`` values; they do
                    # not carry additional proof and must not invalidate the
                    # independently addressable rendered candidate.
                    continue
                else:
                    anchors.append(anchor)
            for candidate in metric.report_candidate_ids:
                alias = str(candidate or "")
                if alias in anchor_aliases:
                    anchors.append(ReportSourceAnchorRef(anchor_id=alias))
                else:
                    candidates.append(alias)
            return PlannedMetric.model_validate({
                **metric.model_dump(),
                "source_anchors": anchors,
                "report_candidate_ids": list(dict.fromkeys(candidates)),
            })

        non_metric_anchors: list[ReportSourceAnchorRef] = []
        non_metric_candidates: list[str] = []
        for anchor in plan.non_metric_source_anchors:
            alias = str(anchor.anchor_id or "")
            if alias in candidate_aliases:
                non_metric_candidates.append(alias)
            else:
                non_metric_anchors.append(anchor)
        for candidate in plan.non_metric_report_candidate_ids:
            alias = str(candidate or "")
            if alias in anchor_aliases:
                non_metric_anchors.append(ReportSourceAnchorRef(anchor_id=alias))
            else:
                non_metric_candidates.append(alias)
        unresolved_outputs: list[UnresolvedEvidenceOutput] = []
        for item in plan.unresolved_outputs:
            output_id = str(item.output_node_id or "").strip()
            anchor = item.source_anchor
            anchor_alias = str(anchor.anchor_id or "").strip() if anchor is not None else ""
            if anchor_alias in candidate_aliases:
                unresolved_outputs.append(UnresolvedEvidenceOutput(
                    output_node_id=anchor_alias,
                    reason=item.reason,
                ))
                continue
            if output_id in anchor_aliases:
                unresolved_outputs.append(UnresolvedEvidenceOutput(
                    source_anchor=ReportSourceAnchorRef(anchor_id=output_id),
                    reason=item.reason,
                ))
                continue
            if not output_id and anchor is not None and not (
                anchor_alias
                or int(anchor.source_line or 0) > 0
                and str(anchor.expression or "").strip()
            ):
                # A shape-only unresolved item classifies no source fact. Drop
                # it here; completeness validation still reports every omitted
                # anchor/candidate instead of treating the empty object as proof.
                continue
            if output_id or anchor is not None:
                unresolved_outputs.append(item)
        return EvidencePlan(
            metrics=[normalize_metric(metric) for metric in plan.metrics],
            unresolved_outputs=unresolved_outputs,
            non_metric_source_anchors=non_metric_anchors,
            non_metric_report_candidate_ids=list(dict.fromkeys(non_metric_candidates)),
        )

    def _restore_resolver_plan_ids(self, plan: EvidencePlan) -> EvidencePlan:
        maps = self._resolver_id_maps
        if not maps:
            return plan

        def unique_items(items: list[Any], key: Callable[[Any], str]) -> list[Any]:
            result: list[Any] = []
            seen: set[str] = set()
            for item in items:
                item_key = key(item)
                if item_key in seen:
                    continue
                seen.add(item_key)
                result.append(item)
            return result

        def restore_metric(metric: PlannedMetric) -> PlannedMetric:
            restored_anchors: list[dict[str, Any]] = []
            restored_candidates: list[str] = []
            for anchor in metric.source_anchors:
                raw_id = str(anchor.anchor_id or "")
                # Resolver aliases are typed. If the planner places a rendered
                # candidate alias in the anchor field, preserve the semantic
                # claim by moving it to the matching typed field instead of
                # treating a recoverable JSON-shape error as planner failure.
                candidate_id = maps.get("report_candidate", {}).get(raw_id)
                if candidate_id:
                    restored_candidates.append(candidate_id)
                    continue
                restored_anchors.append({
                    **anchor.model_dump(),
                    "anchor_id": maps.get("anchor", {}).get(raw_id, raw_id),
                })
            for value in metric.report_candidate_ids:
                raw_id = str(value or "")
                anchor_id = maps.get("anchor", {}).get(raw_id)
                if anchor_id:
                    restored_anchors.append({"anchor_id": anchor_id})
                    continue
                restored_candidates.append(
                    maps.get("report_candidate", {}).get(raw_id, raw_id)
                )
            return PlannedMetric.model_validate({
                **metric.model_dump(),
                "calculation_id": maps.get("calculation", {}).get(
                    str(metric.calculation_id), str(metric.calculation_id)
                ),
                "output_node_ids": [
                    restored
                    for value in metric.output_node_ids
                    for restored in (
                        maps.get("output", {}).get(str(value))
                        or [str(value)]
                    )
                ],
                "operation_node_ids": [
                    maps.get("operation", {}).get(str(value), str(value))
                    for value in metric.operation_node_ids
                ],
                "query_node_ids": [
                    maps.get("query", {}).get(str(value), str(value))
                    for value in metric.query_node_ids
                ],
                "claimed_query_node_ids": [
                    maps.get("query", {}).get(str(value), str(value))
                    for value in metric.claimed_query_node_ids
                ],
                "source_anchors": unique_items(
                    restored_anchors,
                    _stable_json,
                ),
                "report_candidate_ids": list(dict.fromkeys(restored_candidates)),
            })

        non_metric_anchors: list[ReportSourceAnchorRef] = []
        non_metric_candidates: list[str] = []
        for anchor in plan.non_metric_source_anchors:
            raw_id = str(anchor.anchor_id or "")
            candidate_id = maps.get("report_candidate", {}).get(raw_id)
            if candidate_id:
                non_metric_candidates.append(candidate_id)
            else:
                non_metric_anchors.append(ReportSourceAnchorRef.model_validate({
                    **anchor.model_dump(),
                    "anchor_id": maps.get("anchor", {}).get(raw_id, raw_id),
                }))
        for value in plan.non_metric_report_candidate_ids:
            raw_id = str(value or "")
            anchor_id = maps.get("anchor", {}).get(raw_id)
            if anchor_id:
                non_metric_anchors.append(ReportSourceAnchorRef(anchor_id=anchor_id))
            else:
                non_metric_candidates.append(
                    maps.get("report_candidate", {}).get(raw_id, raw_id)
                )
        return EvidencePlan(
            metrics=[restore_metric(metric) for metric in plan.metrics],
            unresolved_outputs=[
                UnresolvedEvidenceOutput(
                    output_node_id=restored,
                    source_anchor=(
                        ReportSourceAnchorRef.model_validate({
                            **item.source_anchor.model_dump(),
                            "anchor_id": maps.get("anchor", {}).get(
                                str(item.source_anchor.anchor_id),
                                str(item.source_anchor.anchor_id),
                            ),
                        })
                        if item.source_anchor is not None
                        else None
                    ),
                    reason=item.reason,
                )
                for item in plan.unresolved_outputs
                for restored in (
                    maps.get("output", {}).get(str(item.output_node_id))
                    or ([str(item.output_node_id)] if str(item.output_node_id) else [""])
                )
            ],
            non_metric_source_anchors=unique_items(
                non_metric_anchors,
                lambda item: str(item.anchor_id or ""),
            ),
            non_metric_report_candidate_ids=list(dict.fromkeys(non_metric_candidates)),
        )

    @staticmethod
    def _planner_datasource_catalog(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        semantic_keys = {
            "title", "display_name", "displayName", "description", "type", "format",
            "unit", "enum", "semantic_type", "semanticType", "business_name", "businessName",
        }
        result: list[dict[str, Any]] = []
        for datasource in context:
            properties = datasource.get("relevant_properties")
            fields: list[dict[str, Any]] = []
            for field, definition in (properties.items() if isinstance(properties, dict) else []):
                compact = {"field": str(field)}
                if isinstance(definition, dict):
                    compact.update({
                        str(key): value
                        for key, value in definition.items()
                        if str(key) in semantic_keys
                        and isinstance(value, (str, int, float, bool, list, tuple, type(None)))
                    })
                fields.append(compact)
            result.append({
                "datasource_id": datasource.get("datasource_id"),
                "datasource_name": datasource.get("datasource_name") or "",
                "datasource_identifier": datasource.get("datasource_identifier") or "",
                "fields": fields,
            })
        return result

    def _invoke_planner(self, payload: dict[str, Any]) -> EvidencePlan:
        if not bool(getattr(Config, "POST_ANALYSIS_EVIDENCE_PLANNER_ENABLED", True)):
            raise RuntimeError("Evidence Planner is disabled")
        runtime_config = create_runtime_config()
        model = create_data_insight_model(runtime_config)
        if runtime_config.adapter == "openai":
            model = model.bind(response_format={"type": "json_object"})
        messages = self._planner_messages(payload)
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
                try:
                    raw_planner_payload = _extract_json_object(response_text)
                except json.JSONDecodeError as exc:
                    response_text = self._repair_planner_json(
                        model,
                        response_text,
                        exc,
                    )
                    raw_planner_payload = _extract_json_object(response_text)
                planner_payload = self._canonicalize_planner_payload(raw_planner_payload)
                plan = EvidencePlan.model_validate(planner_payload)
                # Normalize typed resolver aliases before validating references.
                # The model may correctly choose a rendered candidate but place
                # its ``rc*`` alias in the source-anchor field (or vice versa).
                # This is a recoverable response-shape mismatch, not missing
                # evidence semantics.
                plan = self._normalize_typed_resolver_aliases(plan)
                plan = self._split_distinct_mapping_selector_metrics(plan, payload)
                # Discovery payloads deliberately omit the internal output
                # catalog. A metric without an anchor or a rendered candidate
                # therefore has no posthoc report position and must not make
                # the entire planner result fail validation.
                if "report_candidate_catalog" in payload:
                    plan = EvidencePlan(
                        metrics=[
                            metric.model_copy(update={"output_node_ids": []})
                            for metric in plan.metrics
                            if metric.source_anchors or metric.report_candidate_ids
                        ],
                        unresolved_outputs=plan.unresolved_outputs,
                        non_metric_source_anchors=plan.non_metric_source_anchors,
                        non_metric_report_candidate_ids=(
                            plan.non_metric_report_candidate_ids
                        ),
                    )
                reference_errors = self._discovery_plan_reference_errors(plan, payload)
                if reference_errors:
                    completeness_only = all(
                        str(error).startswith((
                            "verified report source anchors are unclassified:",
                            "rendered report candidates are unclassified:",
                        ))
                        for error in reference_errors
                    )
                    if attempt == 1 and completeness_only:
                        # One focused retry is enough. Remaining candidate
                        # omissions become isolated unresolved positions during
                        # local materialization; they must not erase verified
                        # report, chart, or table evidence.
                        return plan
                    raise ValueError("; ".join(reference_errors[:20]))
                return plan
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 0:
                    if response_text:
                        messages.append(AIMessage(response_text))
                    messages.append(HumanMessage(
                        "上一轮输出不符合报告证据发现契约。请确保源码锚点引用存在，"
                        "同一源码锚点只分类一次，metric 至少包含一个源码锚点或报告候选，"
                        "并引用目录中的真实计算操作，或用 calculation_source_refs 精确定位源码计算表达式。"
                        "只修正 JSON 结构和源码/操作引用："
                        + str(exc)[:1200]
                    ))
                    continue
                raise
        raise RuntimeError(f"Evidence Planner failed: {last_error}")

    @staticmethod
    def _split_distinct_mapping_selector_metrics(
        plan: EvidencePlan,
        payload: dict[str, Any],
    ) -> EvidencePlan:
        """Split an over-broad planner metric along proven mapping branches.

        Rendered loop occurrences carry their concrete slot bindings, while
        mapping-member operations carry the literal selector written by the
        successful program. When both sides agree, the split is deterministic:
        each report occurrence keeps exactly its own calculation operation.
        Unknown or incomplete selector shapes are left untouched for normal
        planner validation.
        """
        report_candidates = {
            str(item.get("candidate_id") or ""): item
            for item in payload.get("report_candidate_catalog") or []
            if str(item.get("candidate_id") or "")
        }
        operations = {
            str(item.get("operation_node_id") or ""): item
            for item in payload.get("operation_catalog") or []
            if str(item.get("operation_node_id") or "")
        }
        normalized_metrics: list[PlannedMetric] = []
        for metric in plan.metrics:
            if metric.source_anchors or len(metric.report_candidate_ids) < 2:
                normalized_metrics.append(metric)
                continue
            operation_selectors = {
                str((operations.get(str(operation_id)) or {}).get(
                    "parameter_bindings", {}
                ).get("mapping_selector") or "")
                for operation_id in metric.operation_node_ids
            }
            operation_selectors.discard("")
            if len(operation_selectors) < 2:
                normalized_metrics.append(metric)
                continue

            candidates_by_selector: dict[str, list[str]] = {}
            for candidate_id in metric.report_candidate_ids:
                bindings = (
                    report_candidates.get(str(candidate_id), {}).get("slot_bindings")
                    or {}
                )
                matches = [
                    str(value)
                    for value in bindings.values()
                    if str(value) in operation_selectors
                ]
                if len(set(matches)) != 1:
                    candidates_by_selector = {}
                    break
                candidates_by_selector.setdefault(matches[0], []).append(
                    str(candidate_id)
                )
            if set(candidates_by_selector) != operation_selectors:
                normalized_metrics.append(metric)
                continue

            operations_by_selector: dict[str, list[str]] = {}
            for operation_id in metric.operation_node_ids:
                selector = str((operations.get(str(operation_id)) or {}).get(
                    "parameter_bindings", {}
                ).get("mapping_selector") or "")
                if selector:
                    operations_by_selector.setdefault(selector, []).append(
                        str(operation_id)
                    )
            if any(
                len(operations_by_selector.get(selector, [])) != 1
                for selector in candidates_by_selector
            ):
                normalized_metrics.append(metric)
                continue

            for selector, candidate_ids in candidates_by_selector.items():
                selector_suffix = _digest(selector, 8)
                business_name = str(metric.business_name or "").strip()
                if selector not in business_name:
                    business_name = f"{selector}{business_name}"
                explanation = str(metric.business_explanation or "").strip()
                if selector not in explanation:
                    explanation = f"{selector}：{explanation}"
                normalized_metrics.append(metric.model_copy(update={
                    "report_candidate_ids": candidate_ids,
                    "operation_node_ids": operations_by_selector[selector],
                    "calculation_source_refs": [],
                    "claimed_query_node_ids": [],
                    "claimed_metric_fields": [],
                    "parameter_bindings": {
                        **metric.parameter_bindings,
                        "mapping_selector": selector,
                    },
                    "metric_key": f"{metric.metric_key}_{selector_suffix}",
                    "business_name": business_name,
                    "business_explanation": explanation,
                }))
        return EvidencePlan(
            metrics=normalized_metrics,
            unresolved_outputs=plan.unresolved_outputs,
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=plan.non_metric_report_candidate_ids,
        )

    @staticmethod
    def _planner_messages(payload: dict[str, Any]) -> list[Any]:
        """Keep the recovery task salient without duplicating source code."""
        code = str(payload.get("analysis_code_with_lines") or "")
        task_facts = {
            key: value
            for key, value in payload.items()
            if key != "analysis_code_with_lines"
        }
        candidate_ids = [
            str(item.get("candidate_id") or "")
            for item in payload.get("report_candidate_catalog") or []
            if str(item.get("candidate_id") or "")
        ]
        anchor_ids = [
            str(item.get("anchor_id") or "")
            for item in payload.get("source_anchor_catalog") or []
            if str(item.get("anchor_id") or "")
        ]
        return [
            SystemMessage(EVIDENCE_PLANNER_PROMPT),
            HumanMessage(
                "以下 JSON 是本次唯一待处理目录及验真辅助事实。目录外表达式已经处理，"
                "不得重新枚举：\n"
                + json.dumps(
                    task_facts,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            ),
            HumanMessage(
                "以下是只读的完整成功 Python 源码。仅用它定位上述目录项的最小计算边界：\n"
                + code
            ),
            HumanMessage(
                "现在只输出 JSON，并逐项分类这些待处理 ID："
                f"report_candidate_ids={candidate_ids}, source_anchor_ids={anchor_ids}。"
                "每个 ID 必须恰好出现一次；目录外源码表达式不要输出。"
            ),
        ]

    @staticmethod
    def _discovery_plan_reference_errors(
        plan: EvidencePlan,
        payload: dict[str, Any],
    ) -> list[str]:
        valid_anchor_ids = {
            str(item.get("anchor_id") or "")
            for item in payload.get("source_anchor_catalog") or []
            if str(item.get("anchor_id") or "")
        }
        valid_operation_ids = {
            str(item.get("operation_node_id") or "")
            for item in payload.get("operation_catalog") or []
            if str(item.get("operation_node_id") or "")
        }
        valid_report_candidate_ids = {
            str(item.get("candidate_id") or "")
            for item in payload.get("report_candidate_catalog") or []
            if str(item.get("candidate_id") or "")
        }
        report_candidates = {
            str(item.get("candidate_id") or ""): item
            for item in payload.get("report_candidate_catalog") or []
            if str(item.get("candidate_id") or "")
        }
        operations = {
            str(item.get("operation_node_id") or ""): item
            for item in payload.get("operation_catalog") or []
            if str(item.get("operation_node_id") or "")
        }
        errors: list[str] = []
        classified: dict[str, str] = {}

        def validate_anchor(
            anchor: ReportSourceAnchorRef,
            owner: str,
            binding_key: str = "",
        ) -> None:
            anchor_id = str(anchor.anchor_id or "")
            identity = anchor_id or (
                f"line:{int(anchor.source_line or 0)}:"
                f"{PostAnalysisEvidenceCompiler._normalized_source_expression(anchor.expression)}"
            )
            if anchor_id:
                if anchor_id not in valid_anchor_ids:
                    errors.append(f"unknown source anchor: {anchor_id}")
            elif not int(anchor.source_line or 0) or not str(anchor.expression or "").strip():
                errors.append(f"{owner} contains an anchor without id or source location")
            if identity:
                identity = f"{identity}|{binding_key}" if binding_key else identity
                previous = classified.get(identity)
                if not previous:
                    classified[identity] = owner

        for index, metric in enumerate(plan.metrics):
            owner = f"metric[{index}]"
            if not metric.source_anchors and not metric.report_candidate_ids:
                errors.append(f"{owner} has no source anchors or report candidates")
            if not metric.operation_node_ids and not metric.calculation_source_refs:
                errors.append(f"{owner} has no calculation operation or source locator")
            binding_key = _stable_json(metric.parameter_bindings or {})
            for anchor in metric.source_anchors:
                validate_anchor(anchor, owner, binding_key)
            for candidate_id in metric.report_candidate_ids:
                candidate_id = str(candidate_id)
                if candidate_id not in valid_report_candidate_ids:
                    errors.append(f"{owner} references unknown report candidate: {candidate_id}")
                previous = classified.get(f"candidate:{candidate_id}")
                if not previous:
                    classified[f"candidate:{candidate_id}"] = owner
            runtime_headings = {
                str(report_candidates.get(str(candidate_id), {}).get("heading") or "").strip()
                for candidate_id in metric.report_candidate_ids
                if report_candidates.get(str(candidate_id), {}).get(
                    "runtime_iteration_boundary_required"
                )
                and str(report_candidates.get(str(candidate_id), {}).get("heading") or "").strip()
            }
            if len(runtime_headings) > 1:
                errors.append(
                    f"{owner} merges independent runtime query occurrences under "
                    f"different headings: {sorted(runtime_headings)}"
                )
            for operation_id in metric.operation_node_ids:
                if str(operation_id) not in valid_operation_ids:
                    errors.append(f"{owner} references unknown operation: {operation_id}")
            for query_id in metric.claimed_query_node_ids:
                if str(query_id) not in {
                    str(item.get("query_node_id") or "")
                    for item in payload.get("query_catalog") or []
                }:
                    errors.append(f"{owner} references unknown claimed query: {query_id}")
            for source_ref in metric.calculation_source_refs:
                if int(source_ref.source_line or 0) <= 0 or not str(source_ref.expression or "").strip():
                    errors.append(f"{owner} contains an invalid calculation source locator")

            operation_selectors = {
                str((operations.get(str(operation_id)) or {}).get("parameter_bindings", {}).get("mapping_selector") or "")
                for operation_id in metric.operation_node_ids
            }
            operation_selectors.discard("")
            candidate_selectors: set[str] = set()
            for candidate_id in metric.report_candidate_ids:
                bindings = (
                    report_candidates.get(str(candidate_id), {}).get("slot_bindings")
                    or {}
                )
                candidate_selectors.update(
                    str(value)
                    for value in bindings.values()
                    if str(value) in operation_selectors
                )
            if len(operation_selectors) > 1 and len(candidate_selectors) > 1:
                errors.append(
                    f"{owner} merges distinct mapping selectors: "
                    f"candidates={sorted(candidate_selectors)} "
                    f"operations={sorted(operation_selectors)}; split one metric per selector"
                )
        for index, anchor in enumerate(plan.non_metric_source_anchors):
            validate_anchor(anchor, f"non_metric[{index}]")
        for index, item in enumerate(plan.unresolved_outputs):
            output_id = str(item.output_node_id or "")
            if output_id:
                if output_id not in valid_report_candidate_ids:
                    errors.append(f"unresolved[{index}] references unknown report candidate: {output_id}")
                else:
                    classified.setdefault(f"candidate:{output_id}", f"unresolved[{index}]")
            if item.source_anchor is not None:
                validate_anchor(item.source_anchor, f"unresolved[{index}]")
        for candidate_id in plan.non_metric_report_candidate_ids:
            candidate_id = str(candidate_id)
            if candidate_id not in valid_report_candidate_ids:
                errors.append(f"non_metric references unknown report candidate: {candidate_id}")
                continue
            if report_candidates.get(candidate_id, {}).get(
                "runtime_iteration_boundary_required"
            ):
                errors.append(
                    "query-backed runtime report candidate cannot be classified "
                    f"as non-metric: {candidate_id}"
                )
                continue
            previous = classified.get(f"candidate:{candidate_id}")
            if not previous:
                classified[f"candidate:{candidate_id}"] = "non_metric"
        required_report_anchor_ids = {
            str(item.get("anchor_id") or "")
            for item in payload.get("source_anchor_catalog") or []
            if item.get("verified_report_output")
            and str(item.get("anchor_id") or "")
        }
        classified_anchor_ids = {
            identity.split("|", 1)[0]
            for identity in classified
            if not identity.startswith("line:")
        }
        missing_anchor_ids = required_report_anchor_ids - classified_anchor_ids
        if missing_anchor_ids:
            errors.append(
                "verified report source anchors are unclassified: "
                f"{sorted(missing_anchor_ids)}"
            )
        missing_candidate_ids = valid_report_candidate_ids - {
            identity.removeprefix("candidate:")
            for identity in classified
            if identity.startswith("candidate:")
        }
        if missing_candidate_ids:
            errors.append(
                "rendered report candidates are unclassified: "
                f"{sorted(missing_candidate_ids)}"
            )
        return errors

    @staticmethod
    def _repair_planner_json(model: Any, response_text: str, error: Exception) -> str:
        """Repair serialization only; never rerun evidence reasoning in this call."""
        repair_messages = [
            SystemMessage(
                "你是 JSON 语法修复器。只修复输入对象的引号、逗号、括号和转义，"
                "不得增加、删除、合并或改写任何 metric、output、operation、query、"
                "字段值或业务说明。只输出修复后的一个 JSON 对象。"
            ),
            HumanMessage(
                _stable_json({
                    "parse_error": str(error)[:500],
                    "malformed_json": response_text,
                })
            ),
        ]
        response = model.invoke(repair_messages)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content or "")

    @staticmethod
    def _canonicalize_planner_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize optional resolver collections without changing its decisions."""
        normalized = dict(payload)
        if not isinstance(normalized.get("metrics"), list):
            normalized["metrics"] = []
        else:
            normalized["metrics"] = [
                {
                    str(key): value
                    for key, value in metric.items()
                    if str(key) != "metric_source_fields"
                }
                if isinstance(metric, dict)
                else metric
                for metric in normalized["metrics"]
                if not isinstance(metric, dict)
                or metric.get("source_anchors")
                or metric.get("report_candidate_ids")
                or metric.get("output_node_ids")
            ]
        if not isinstance(normalized.get("unresolved_outputs"), list):
            normalized["unresolved_outputs"] = []
        return normalized

    @staticmethod
    def _normalize_unresolved_report_outputs(plan: EvidencePlan) -> EvidencePlan:
        claimed_output_ids = {
            str(output_id)
            for metric in plan.metrics
            for output_id in metric.output_node_ids
            if str(output_id)
        }
        seen: set[tuple[str, str, int, str]] = set()
        unresolved: list[UnresolvedEvidenceOutput] = []
        for item in plan.unresolved_outputs:
            output_id = str(item.output_node_id or "")
            if output_id and output_id in claimed_output_ids:
                continue
            anchor = item.source_anchor
            identity = (
                output_id,
                str(anchor.anchor_id or "") if anchor is not None else "",
                int(anchor.source_line or 0) if anchor is not None else 0,
                str(anchor.expression or "") if anchor is not None else "",
            )
            if identity in seen:
                continue
            seen.add(identity)
            unresolved.append(item)
        return EvidencePlan(
            metrics=plan.metrics,
            unresolved_outputs=unresolved,
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=plan.non_metric_report_candidate_ids,
        )

    @staticmethod
    def _isolate_duplicate_report_output_claims(
        plan: EvidencePlan,
        calculation_candidates: dict[str, dict[str, Any]],
    ) -> EvidencePlan:
        """Keep one locally best-proven owner or isolate only the conflicting slot."""
        claims: dict[str, list[int]] = {}
        for metric_index, metric in enumerate(plan.metrics):
            for output_id in metric.output_node_ids:
                claims.setdefault(str(output_id), []).append(metric_index)
        duplicates = {
            output_id: owners
            for output_id, owners in claims.items()
            if output_id and len(owners) > 1
        }
        if not duplicates:
            return plan

        remove_by_metric: dict[int, set[str]] = {}
        unresolved = list(plan.unresolved_outputs)
        for output_id, owners in duplicates.items():
            hint = next((
                (candidate.get("local_lineage_hints") or {}).get(output_id)
                for candidate in calculation_candidates.values()
                if (candidate.get("local_lineage_hints") or {}).get(output_id)
            ), {}) or {}
            hint_operations = set(hint.get("operation_node_ids") or [])
            hint_bindings = dict(hint.get("parameter_bindings") or {})
            scores: dict[int, tuple[int, int, int]] = {}
            for owner in owners:
                metric = plan.metrics[owner]
                metric_operations = set(metric.operation_node_ids)
                binding_match = int(
                    bool(hint_bindings)
                    and dict(metric.parameter_bindings or {}) == hint_bindings
                )
                exact_operations = int(
                    bool(hint_operations)
                    and metric_operations == hint_operations
                )
                operation_overlap = len(metric_operations.intersection(hint_operations))
                scores[owner] = (binding_match, exact_operations, operation_overlap)
            best_score = max(scores.values())
            winners = [owner for owner, score in scores.items() if score == best_score]
            keep_owner = winners[0] if len(winners) == 1 and best_score > (0, 0, 0) else None
            for owner in owners:
                if owner != keep_owner:
                    remove_by_metric.setdefault(owner, set()).add(output_id)
            if keep_owner is None:
                unresolved.append(UnresolvedEvidenceOutput(
                    output_node_id=output_id,
                    reason="Multiple semantic claims could not be reduced to one proven calculation boundary.",
                ))

        metrics: list[PlannedMetric] = []
        for metric_index, metric in enumerate(plan.metrics):
            retained_outputs = [
                output_id for output_id in metric.output_node_ids
                if output_id not in remove_by_metric.get(metric_index, set())
            ]
            if not retained_outputs:
                continue
            metrics.append(PlannedMetric.model_validate({
                **metric.model_dump(),
                "output_node_ids": retained_outputs,
            }))
        return EvidencePlan(
            metrics=metrics,
            unresolved_outputs=unresolved,
            non_metric_source_anchors=plan.non_metric_source_anchors,
            non_metric_report_candidate_ids=plan.non_metric_report_candidate_ids,
        )

    @staticmethod
    def _sanitize_semantic_plan(
        plan: EvidencePlan,
        calculation_candidates: dict[str, dict[str, Any]],
    ) -> EvidencePlan:
        """Isolate unprovable semantic bindings instead of dropping the whole plan."""
        metrics: list[PlannedMetric] = []
        unresolved = list(plan.unresolved_outputs)
        unresolved_ids = {
            str(item.output_node_id or "").strip()
            for item in unresolved
            if str(item.output_node_id or "").strip()
        }
        for metric in plan.metrics:
            candidate = calculation_candidates.get(str(metric.calculation_id or ""))
            if not candidate or not candidate.get("semantic_scope"):
                metrics.append(metric)
                continue
            output_ids = [
                str(output_id).strip()
                for output_id in metric.output_node_ids
                if str(output_id).strip()
            ]
            output_operation_ids = candidate.get("output_operation_ids") or {}
            local_hints = candidate.get("local_lineage_hints") or {}
            requested_operations = {
                str(operation_id)
                for operation_id in metric.operation_node_ids
                if str(operation_id)
            }
            operation_query_ids = candidate.get("operation_query_ids") or {}
            lineage_fields_by_boundary: dict[str, set[tuple[str, ...]]] = {}
            for output_id, hint in local_hints.items():
                boundary_key = str(
                    hint.get("local_boundary_key")
                    or hint.get("calculation_id")
                    or output_id
                )
                lineage_signature = tuple(sorted(
                    str(field)
                    for field in hint.get("output_lineage_fields") or []
                    if str(field)
                ))
                if boundary_key and lineage_signature:
                    lineage_fields_by_boundary.setdefault(
                        boundary_key,
                        set(),
                    ).add(lineage_signature)
            canonical_hints: dict[str, dict[str, Any]] = {}
            partitions: dict[tuple[str, str], list[str]] = {}
            unresolved_output_ids: list[str] = []
            for output_id in output_ids:
                hint = local_hints.get(output_id) or {}
                hint_operations = [
                    str(operation_id)
                    for operation_id in hint.get("operation_node_ids") or []
                    if str(operation_id)
                ]
                hint_queries = [
                    str(query_id)
                    for query_id in hint.get("query_node_ids") or []
                    if str(query_id)
                ]
                has_canonical_hint = bool(
                    hint_operations
                    and hint_queries
                    and not hint.get("ambiguous")
                    and requested_operations.intersection(hint_operations)
                    and len(
                        lineage_fields_by_boundary.get(
                            str(
                                hint.get("local_boundary_key")
                                or hint.get("calculation_id")
                                or output_id
                            ),
                            set(),
                        )
                    ) == 1
                )
                reaches_selected_operation = bool(
                    requested_operations.intersection(
                        str(operation_id)
                        for operation_id in output_operation_ids.get(output_id) or []
                    )
                )
                if not has_canonical_hint and not reaches_selected_operation:
                    unresolved_output_ids.append(output_id)
                    continue
                if has_canonical_hint:
                    boundary_key = str(
                        hint.get("local_boundary_key")
                        or hint.get("calculation_id")
                        or output_id
                    )
                    canonical_hints[output_id] = hint
                    partitions.setdefault(("local", boundary_key), []).append(output_id)
                else:
                    partitions.setdefault(("semantic", "selected"), []).append(output_id)

            for (partition_kind, _), partition_output_ids in partitions.items():
                allowed_operations = {
                    str(operation_id)
                    for output_id in partition_output_ids
                    for operation_id in output_operation_ids.get(output_id) or []
                    if str(operation_id)
                }
                if partition_kind == "local":
                    selected_operations = list(dict.fromkeys(
                        str(operation_id)
                        for output_id in partition_output_ids
                        for operation_id in (
                            canonical_hints.get(output_id, {}).get("operation_node_ids") or []
                        )
                        if str(operation_id) in allowed_operations
                    ))
                    selected_queries = list(dict.fromkeys(
                        str(query_id)
                        for operation_id in selected_operations
                        for query_id in operation_query_ids.get(operation_id) or []
                        if str(query_id)
                    ))
                else:
                    selected_operations = [
                        str(operation_id)
                        for operation_id in metric.operation_node_ids
                        if str(operation_id) in allowed_operations
                    ]
                    reachable_queries = list(dict.fromkeys(
                        str(query_id)
                        for operation_id in selected_operations
                        for query_id in operation_query_ids.get(operation_id) or []
                        if str(query_id)
                    ))
                    # Query membership is factual lineage, not a semantic choice.
                    # Once the planner selects the calculation operations, consume
                    # every query those operations actually reach.
                    selected_queries = reachable_queries
                if selected_operations and selected_queries:
                    metric_payload = {
                        **metric.model_dump(),
                        "output_node_ids": partition_output_ids,
                        "operation_node_ids": list(dict.fromkeys(selected_operations)),
                        "query_node_ids": list(dict.fromkeys(selected_queries)),
                    }
                    if partition_kind == "local":
                        local_hint = canonical_hints.get(partition_output_ids[0]) or {}
                        metric_payload.update({
                            "metric_source_fields": list(dict.fromkeys(
                                str(field)
                                for field in local_hint.get("metric_source_fields") or []
                                if str(field)
                            )),
                            "parameter_bindings": dict(
                                local_hint.get("parameter_bindings") or {}
                            ),
                        })
                    metrics.append(PlannedMetric.model_validate({
                        **metric_payload,
                    }))
                else:
                    unresolved_output_ids.extend(partition_output_ids)
            for output_id in unresolved_output_ids:
                if output_id in unresolved_ids:
                    continue
                unresolved.append(UnresolvedEvidenceOutput(
                    output_node_id=output_id,
                    reason="代码路径无法证明该报告数值来自所选查询。",
                ))
                unresolved_ids.add(output_id)
        return EvidencePlan(
            metrics=metrics,
            unresolved_outputs=unresolved,
            non_metric_report_candidate_ids=plan.non_metric_report_candidate_ids,
        )

    @staticmethod
    def _plan_reference_errors(
        plan: EvidencePlan,
        calculation_candidates: dict[str, dict[str, Any]],
        *,
        require_complete: bool = True,
    ) -> list[str]:
        expected_output_ids = {
            str(output_id)
            for candidate in calculation_candidates.values()
            for output_id in candidate.get("output_node_ids") or []
            if str(output_id)
        }
        errors: list[str] = []
        claimed_output_ids: set[str] = set()
        for metric_index, metric in enumerate(plan.metrics):
            calculation_id = str(metric.calculation_id or "").strip()
            candidate = calculation_candidates.get(calculation_id)
            if candidate is None:
                errors.append(f"metrics[{metric_index}].calculation_id is unknown: {calculation_id}")
                continue
            allowed_output_ids = {
                str(item) for item in candidate.get("output_node_ids") or [] if str(item)
            }
            selected_output_ids = [
                str(item).strip() for item in metric.output_node_ids if str(item).strip()
            ]
            if not selected_output_ids:
                errors.append(f"metrics[{metric_index}].output_node_ids is empty")
                continue
            unknown_output_ids = set(selected_output_ids) - allowed_output_ids
            if unknown_output_ids:
                errors.append(
                    f"metrics[{metric_index}] references outputs outside calculation candidate: "
                    f"{sorted(unknown_output_ids)}"
                )
            allowed_operation_ids = {
                str(item) for item in candidate.get("operation_node_ids") or [] if str(item)
            }
            selected_operation_ids = {
                str(item).strip() for item in metric.operation_node_ids if str(item).strip()
            }
            if not selected_operation_ids:
                errors.append(f"metrics[{metric_index}].operation_node_ids is empty")
            elif not selected_operation_ids.issubset(allowed_operation_ids):
                errors.append(
                    f"metrics[{metric_index}] references operations outside calculation candidate: "
                    f"{sorted(selected_operation_ids - allowed_operation_ids)}"
                )
            if candidate.get("semantic_scope"):
                output_operation_ids = candidate.get("output_operation_ids") or {}
                output_reachable_operation_ids = {
                    str(operation_id)
                    for output_id in selected_output_ids
                    for operation_id in output_operation_ids.get(output_id) or []
                    if str(operation_id)
                }
                unreachable_operations = (
                    selected_operation_ids - output_reachable_operation_ids
                )
                # An empty reachable set means the local syntax index could not
                # prove this edge. It is unproven, not contradicted, and may be
                # supplied by an exact LLM source locator. Reject only when the
                # local index has a positive reachability fact that conflicts
                # with the selected operation.
                if output_reachable_operation_ids and unreachable_operations:
                    errors.append(
                        f"metrics[{metric_index}] selects operations that do not reach its outputs: "
                        f"{sorted(unreachable_operations)}"
                    )
            allowed_query_ids = {
                str(item) for item in candidate.get("query_node_ids") or [] if str(item)
            }
            selected_query_ids = {
                str(item).strip() for item in metric.query_node_ids if str(item).strip()
            }
            if not selected_query_ids:
                errors.append(f"metrics[{metric_index}].query_node_ids is empty")
            elif not selected_query_ids.issubset(allowed_query_ids):
                errors.append(
                    f"metrics[{metric_index}] references queries outside calculation candidate: "
                    f"{sorted(selected_query_ids - allowed_query_ids)}"
                )
            operation_query_ids = (
                candidate.get("operation_query_ids")
                if isinstance(candidate.get("operation_query_ids"), dict)
                else {}
            )
            reachable_query_ids = {
                str(query_id)
                for operation_id in selected_operation_ids
                for query_id in operation_query_ids.get(operation_id) or []
                if str(query_id)
            }
            if (
                reachable_query_ids
                and not selected_query_ids.issubset(reachable_query_ids)
            ):
                errors.append(
                    f"metrics[{metric_index}] selects queries not reachable from its operations: "
                    f"{sorted(selected_query_ids - reachable_query_ids)}"
                )
            if candidate.get("ambiguous_input"):
                family_by_operation = candidate.get("operation_family_signatures") or {}
                write_by_operation = candidate.get("operation_write_signatures") or {}
                semantic_kind_by_operation = (
                    candidate.get("operation_semantic_kinds") or {}
                )
                selected_families = {
                    str(family_by_operation.get(operation_id) or "")
                    for operation_id in selected_operation_ids
                    if str(family_by_operation.get(operation_id) or "")
                }
                selected_write_targets = {
                    str(write_by_operation.get(operation_id) or "")
                    for operation_id in selected_operation_ids
                    if str(write_by_operation.get(operation_id) or "")
                }
                selected_semantic_kinds = {
                    str(semantic_kind_by_operation.get(operation_id) or "")
                    for operation_id in selected_operation_ids
                    if str(semantic_kind_by_operation.get(operation_id) or "")
                }
                ranking_selection = (
                    "ranking" in selected_semantic_kinds
                    and selected_semantic_kinds.issubset({
                        "ranking", "projection", "formula", "aggregation",
                    })
                )
                if (
                    len(selected_families) > 1
                    and len(selected_write_targets) != 1
                    and not ranking_selection
                ):
                    errors.append(
                        f"metrics[{metric_index}] did not resolve the ambiguous calculation boundary"
                    )
            allowed_metric_fields = {
                str(item).strip()
                for item in (
                    candidate.get("available_metric_fields")
                    or candidate.get("metric_source_fields")
                    or []
                )
                if str(item).strip()
            }
            selected_metric_fields = {
                str(item).strip() for item in metric.metric_source_fields if str(item).strip()
            }
            if (
                metric.lineage_verification != "attested"
                and not selected_metric_fields.issubset(allowed_metric_fields)
            ):
                errors.append(
                    f"metrics[{metric_index}] references fields outside calculation candidate: "
                    f"{sorted(selected_metric_fields - allowed_metric_fields)}"
                )
            duplicate_output_ids = set(selected_output_ids).intersection(claimed_output_ids)
            if duplicate_output_ids:
                errors.append(f"outputs are claimed more than once: {sorted(duplicate_output_ids)}")
            claimed_output_ids.update(selected_output_ids)

        unresolved_output_ids: set[str] = set()
        required_output_ids = {
            str(output_id)
            for candidate in calculation_candidates.values()
            if bool(candidate.get("evidence_required"))
            and not bool(candidate.get("ambiguity_requires_resolution"))
            for output_id in candidate.get("output_node_ids") or []
            if str(output_id)
        }
        for unresolved_index, unresolved in enumerate(plan.unresolved_outputs):
            output_id = str(unresolved.output_node_id or "").strip()
            if not output_id and unresolved.source_anchor is not None:
                if not str(unresolved.reason or "").strip():
                    errors.append(f"unresolved_outputs[{unresolved_index}].reason is empty")
                continue
            if output_id not in expected_output_ids:
                if not require_complete:
                    continue
                errors.append(
                    f"unresolved_outputs[{unresolved_index}].output_node_id is unknown: {output_id}"
                )
                continue
            if not str(unresolved.reason or "").strip():
                errors.append(f"unresolved_outputs[{unresolved_index}].reason is empty")
            if output_id in unresolved_output_ids or output_id in claimed_output_ids:
                errors.append(f"output is resolved more than once: {output_id}")
            if output_id in required_output_ids:
                errors.append(
                    f"unresolved_outputs[{unresolved_index}] is evidence_required: {output_id}"
                )
            unresolved_output_ids.add(output_id)

        if require_complete:
            missing_output_ids = expected_output_ids - claimed_output_ids - unresolved_output_ids
            if missing_output_ids:
                errors.append(f"missing output bindings: {sorted(missing_output_ids)}")
        return errors

    @staticmethod
    def _expand_operation_ids(
        operation_ids: list[str],
        query_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        *,
        available_operation_ids: list[str] | None = None,
        metric_fields: list[str] | None = None,
    ) -> list[str]:
        selected = set(operation_ids)
        available = set(available_operation_ids or operation_by_id)
        field_set = {str(field).strip() for field in metric_fields or [] if str(field).strip()}
        producers: dict[str, list[str]] = {}
        for operation_id, operation in operation_by_id.items():
            if operation_id not in available:
                continue
            for target in operation.get("targets") or []:
                target_name = str(target).strip()
                if target_name:
                    producers.setdefault(target_name, []).append(operation_id)

        # Keep the exact local dependency closure for selected calculations.
        # This is especially important when an LLM attests the query boundary
        # for a report slot: the attestation must not discard the numerator,
        # denominator, or other verified assignments needed to explain the
        # formula. For reassigned variables, use the nearest preceding writer
        # rather than merging unrelated historical values.
        pending = list(selected)
        while pending:
            operation_id = pending.pop()
            operation = operation_by_id.get(operation_id) or {}
            current_line = int(operation.get("line") or 0)
            for loaded_name in operation.get("loads") or []:
                candidates = [
                    producer_id
                    for producer_id in producers.get(str(loaded_name), [])
                    if producer_id != operation_id
                    and (
                        not current_line
                        or int((operation_by_id.get(producer_id) or {}).get("line") or 0)
                        <= current_line
                    )
                ]
                if not candidates:
                    continue
                producer_id = max(
                    candidates,
                    key=lambda item: (
                        int((operation_by_id.get(item) or {}).get("line") or 0),
                        item,
                    ),
                )
                if producer_id in selected:
                    continue
                selected.add(producer_id)
                pending.append(producer_id)
        for operation_id, operation in operation_by_id.items():
            if operation_id not in available:
                continue
            if operation.get("kind") == "validated_query" and str(operation.get("query_node_id")) in query_ids:
                selected.add(operation_id)
                continue
            if operation.get("kind") == "query_projection" and str(operation.get("query_node_id")) in query_ids:
                targets = {
                    str(target).strip() for target in operation.get("targets") or []
                    if str(target).strip()
                }
                if targets.intersection(field_set):
                    selected.add(operation_id)
        return sorted(
            selected,
            key=lambda item: (
                int((operation_by_id.get(item) or {}).get("line") or 0),
                item,
            ),
        )

    @staticmethod
    def _selected_operation_metric_fields(
        operation_ids: list[str],
        query_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        query_field_names = {
            str(target).strip()
            for operation in operation_by_id.values()
            if operation.get("kind") in {"query_projection", "source_field"}
            and str(operation.get("query_node_id") or "") in query_ids
            for target in operation.get("targets") or []
            if str(target).strip()
        }
        mentioned_fields: list[str] = []
        for operation_id in operation_ids:
            source = str((operation_by_id.get(operation_id) or {}).get("source") or "")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            mentioned_fields.extend(
                str(item.value).strip()
                for item in ast.walk(tree)
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and str(item.value).strip() in query_field_names
            )
        return list(dict.fromkeys(mentioned_fields))

    @staticmethod
    def _operation_formula(
        operation_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        *,
        boundary_operation_ids: list[str] | None = None,
    ) -> str:
        formula, _ = PostAnalysisEvidenceCompiler._operation_formula_details(
            operation_ids,
            operation_by_id,
            boundary_operation_ids=boundary_operation_ids,
        )
        return formula

    @staticmethod
    def _operation_formula_details(
        operation_ids: list[str],
        operation_by_id: dict[str, dict[str, Any]],
        *,
        boundary_operation_ids: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Render a verified formula operation as a concise business expression."""
        operations = sorted(
            (
                operation_by_id[operation_id]
                for operation_id in operation_ids
                if operation_id in operation_by_id
            ),
            key=lambda item: int(item.get("line") or 0),
        )
        boundary_ids = set(boundary_operation_ids or operation_ids)
        formula_operations = [
            operation
            for operation in operations
            if str(operation.get("operation_node_id") or "") in boundary_ids
            if operation.get("semantic_kind") == "formula"
        ]
        locally_parsed_formula_operations = [
            operation
            for operation in formula_operations
            if not operation.get("planner_attested")
        ]
        if locally_parsed_formula_operations:
            formula_operations = locally_parsed_formula_operations
        if not formula_operations:
            return "", []

        assignments: dict[str, ast.AST] = {}
        field_aliases: dict[str, str] = {}
        parsed_operations: list[tuple[dict[str, Any], ast.AST]] = []
        for operation in operations:
            try:
                tree = ast.parse(str(operation.get("source") or ""))
            except SyntaxError:
                continue
            parsed_operations.append((operation, tree))
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                if not isinstance(call.func, ast.Attribute) or call.func.attr != "agg":
                    continue
                for keyword in call.keywords:
                    if not keyword.arg or not isinstance(keyword.value, (ast.Tuple, ast.List)):
                        continue
                    values = list(keyword.value.elts)
                    if (
                        values
                        and isinstance(values[0], ast.Constant)
                        and isinstance(values[0].value, str)
                    ):
                        field_aliases[str(keyword.arg)] = str(values[0].value)
            for statement in tree.body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            assignments[target.id] = statement.value
                elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    assignments[statement.target.id] = statement.value

        formula_expression: ast.AST | None = None
        formula_operation_ids = {
            str(operation.get("operation_node_id") or "")
            for operation in formula_operations
        }
        for operation, tree in parsed_operations:
            if str(operation.get("operation_node_id") or "") not in formula_operation_ids:
                continue
            for statement in tree.body:
                if isinstance(statement, ast.Assign):
                    formula_expression = statement.value
                elif isinstance(statement, ast.AnnAssign):
                    formula_expression = statement.value
                elif isinstance(statement, ast.Expr):
                    formula_expression = statement.value
            if formula_expression is not None:
                break
        if formula_expression is None:
            return "", []

        binary_symbols = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "×",
            ast.Div: "÷",
            ast.FloorDiv: "//",
            ast.Mod: "%",
            ast.Pow: "^",
        }
        comparison_symbols = {
            ast.Eq: "=",
            ast.NotEq: "≠",
            ast.Lt: "<",
            ast.LtE: "≤",
            ast.Gt: ">",
            ast.GtE: "≥",
        }
        aggregate_names = {
            "sum": "SUM",
            "mean": "AVG",
            "avg": "AVG",
            "min": "MIN",
            "max": "MAX",
            "count": "COUNT",
            "nunique": "DISTINCT_COUNT",
        }

        class FormulaRenderError(ValueError):
            pass

        formula_fields: list[str] = []

        def render(node: ast.AST, resolving: frozenset[str] = frozenset()) -> str:
            if isinstance(node, ast.IfExp):
                return render(node.body, resolving)
            if isinstance(node, ast.Name):
                if node.id in assignments and node.id not in resolving:
                    return render(assignments[node.id], resolving | {node.id})
                if "_" not in node.id and not node.id.isascii():
                    return node.id
                raise FormulaRenderError(f"unresolved formula variable: {node.id}")
            if isinstance(node, ast.Constant):
                return str(node.value)
            if isinstance(node, ast.Subscript):
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    field = str(key.value)
                    resolved_field = field_aliases.get(field, field)
                    formula_fields.append(resolved_field)
                    return resolved_field
                raise FormulaRenderError("dynamic formula field")
            if isinstance(node, ast.BinOp):
                symbol = binary_symbols.get(type(node.op))
                if not symbol:
                    raise FormulaRenderError("unsupported formula operator")
                return f"({render(node.left, resolving)} {symbol} {render(node.right, resolving)})"
            if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
                symbol = comparison_symbols.get(type(node.ops[0]))
                if not symbol:
                    raise FormulaRenderError("unsupported comparison operator")
                return (
                    f"{render(node.left, resolving)} {symbol} "
                    f"{render(node.comparators[0], resolving)}"
                )
            if isinstance(node, ast.UnaryOp):
                symbol = "-" if isinstance(node.op, ast.USub) else "+"
                return f"{symbol}{render(node.operand, resolving)}"
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"int", "float"}
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    return render(node.args[0], resolving)
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "len"
                    and len(node.args) == 1
                ):
                    return "COUNT(记录)"
                if isinstance(node.func, ast.Attribute):
                    function_name = aggregate_names.get(node.func.attr.lower())
                    if function_name:
                        return f"{function_name}({render(node.func.value, resolving)})"
                    if node.func.attr in {"fillna", "dropna", "astype"}:
                        return render(node.func.value, resolving)
                    if (
                        node.func.attr == "to_numeric"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "pd"
                        and node.args
                    ):
                        return render(node.args[0], resolving)
                if isinstance(node.func, ast.Name) and node.func.id in {"abs", "round"}:
                    arguments = "、".join(render(argument, resolving) for argument in node.args)
                    return f"{node.func.id.upper()}({arguments})"
                raise FormulaRenderError("unsupported formula function")
            raise FormulaRenderError(f"unsupported formula syntax: {type(node).__name__}")

        try:
            formula = render(formula_expression).strip()
        except FormulaRenderError:
            return "", []
        if formula.startswith("(") and formula.endswith(")"):
            formula = formula[1:-1]
        return formula[:500], list(dict.fromkeys(formula_fields))

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

        def output_metric_fields(output: dict[str, Any]) -> list[str]:
            lineage_fields = [
                str(field).strip()
                for field in output.get("lineage_fields") or []
                if str(field).strip()
            ]
            if lineage_fields:
                return list(dict.fromkeys(lineage_fields))
            field = str(output.get("field") or "").strip()
            return [field] if field else []

        for metric in plan.metrics:
            candidate = calculation_candidates.get(str(metric.calculation_id or "")) or {}
            output_ids = list(dict.fromkeys(
                str(item).strip() for item in metric.output_node_ids if str(item).strip()
            ))
            selected_outputs = [output_by_id[item] for item in output_ids if item in output_by_id]
            candidate_operation_ids = list(dict.fromkeys(
                str(operation_id)
                for operation_id in metric.operation_node_ids
                if str(operation_id) in operation_by_id
            ))
            query_ids = list(dict.fromkeys(
                str(query_id)
                for query_id in metric.query_node_ids
                if str(query_id) in query_by_id
            ))
            metric_fields = list(dict.fromkeys(
                metric.metric_source_fields
                or candidate.get("metric_source_fields")
                or (
                    [
                        field
                        for item in output_ids
                        for field in output_metric_fields(output_by_id[item])
                    ]
                    if candidate.get("semantic_scope")
                    else []
                )
                or (
                    self._selected_operation_metric_fields(
                        candidate_operation_ids,
                        query_ids,
                        operation_by_id,
                    )
                    if candidate.get("recovery_candidate")
                    else []
                )
                or [
                    field
                    for item in output_ids
                    for field in output_metric_fields(output_by_id[item])
                ]
            ))
            operation_ids = self._expand_operation_ids(
                candidate_operation_ids,
                query_ids,
                operation_by_id,
                available_operation_ids=(
                    list(operation_by_id)
                    if candidate.get("semantic_scope")
                    or metric.lineage_verification == "attested"
                    else list(candidate.get("operation_node_ids") or [])
                ),
                metric_fields=metric_fields,
            )
            if not output_ids or not operation_ids or not query_ids:
                continue
            if any(item not in output_by_id for item in output_ids):
                continue
            if any(item not in operation_by_id for item in operation_ids):
                continue
            if any(item not in query_by_id for item in query_ids):
                continue
            boundary_operation_ids = list(dict.fromkeys([
                str(operation_id)
                for output in selected_outputs
                for operation_id in output.get("boundary_operation_node_ids") or []
                if str(operation_id) in operation_by_id
            ]))
            if not boundary_operation_ids:
                boundary_operation_ids = [
                    str(operation_id)
                    for operation_id in candidate.get("boundary_operation_node_ids") or []
                    if str(operation_id) in operation_by_id
                ]
            if not boundary_operation_ids:
                boundary_operation_ids = candidate_operation_ids[:1]
            formula, formula_fields = self._operation_formula_details(
                operation_ids,
                operation_by_id,
                boundary_operation_ids=boundary_operation_ids,
            )
            if not formula:
                # Render helpers and nested report builders can make the
                # output-adjacent boundary a presentation expression such as
                # ``pct(rate)``. The planner may still have located the exact
                # verified rate assignment. Prefer that calculation boundary
                # before giving up the user-readable formula.
                formula_operation_ids = [
                    operation_id
                    for operation_id in candidate_operation_ids
                    if (operation_by_id.get(operation_id) or {}).get(
                        "semantic_kind"
                    ) == "formula"
                ]
                if formula_operation_ids:
                    formula, formula_fields = self._operation_formula_details(
                        operation_ids,
                        operation_by_id,
                        boundary_operation_ids=formula_operation_ids,
                    )
            query_field_names = {
                str(target).strip()
                for operation in operation_by_id.values()
                if operation.get("kind") in {"query_projection", "source_field"}
                and str(operation.get("query_node_id") or "") in query_ids
                for target in operation.get("targets") or []
                if str(target).strip()
            }
            formula_source_fields = [
                field for field in formula_fields if field in query_field_names
            ]
            if formula_source_fields:
                metric_fields = formula_source_fields
                operation_ids = self._expand_operation_ids(
                    candidate_operation_ids,
                    query_ids,
                    operation_by_id,
                    available_operation_ids=(
                        list(operation_by_id)
                        if candidate.get("semantic_scope")
                        or metric.lineage_verification == "attested"
                        else list(candidate.get("operation_node_ids") or [])
                    ),
                    metric_fields=metric_fields,
                )
            signature_payload = {
                "queries": sorted(query_ids),
                "operations": [operation_by_id[item]["source"] for item in sorted(operation_ids)],
                "metric_fields": sorted(metric_fields),
                "lineage_verification": metric.lineage_verification,
                "parameter_bindings": (
                    metric.parameter_bindings
                    or candidate.get("parameter_bindings")
                    or {}
                ),
            }
            signature = _digest(_stable_json(signature_payload), 24)
            if any(output_claims.get(item) not in (None, signature) for item in output_ids):
                continue
            evidence_key = f"calc_{signature}"
            for output_id in output_ids:
                output_claims[output_id] = signature
            if signature not in definitions_by_signature:
                boundary = {}
                if boundary_operation_ids:
                    boundary = operation_by_id.get(boundary_operation_ids[0]) or {}
                definitions_by_signature[signature] = {
                    "evidence_key": evidence_key,
                    "metric_name": metric.business_name.strip() or metric.metric_key,
                    "value_type": metric.value_type,
                    "unit": metric.display_unit,
                    "query_refs": query_ids,
                    "derivation": {
                        "mode": (
                            "formula"
                            if formula
                            else self._derivation_mode(boundary.get("semantic_kind"))
                        ),
                        "description": metric.business_explanation.strip(),
                    },
                    "calculation": {
                        "metric_fields": metric_fields,
                        "formula": formula,
                        "calculation_signature": signature,
                        "operation_node_ids": operation_ids,
                        "lineage_verification": metric.lineage_verification,
                        "parameter_bindings": (
                            metric.parameter_bindings
                            or candidate.get("parameter_bindings")
                            or {}
                        ),
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
                    point_selector = output.get("point_selector")
                    chart_refs.append({
                        "chart_index": int(output.get("artifact_index") or 0),
                        "artifact_id": int(output.get("artifact_id") or 0),
                        "value_field": output.get("field"),
                        "point_scope": (
                            "selector"
                            if isinstance(point_selector, dict) and point_selector
                            else "all"
                        ),
                        **(
                            {"point_selector": point_selector}
                            if isinstance(point_selector, dict) and point_selector
                            else {}
                        ),
                        "evidence_key": evidence_key,
                        "display_unit": metric.display_unit,
                    })
                elif artifact_type == "table":
                    row_selector = output.get("row_selector")
                    table_refs.append({
                        "table_index": int(output.get("artifact_index") or 0),
                        "artifact_id": int(output.get("artifact_id") or 0),
                        "column_key": output.get("field"),
                        "row_scope": "selector" if isinstance(row_selector, dict) and row_selector else "all",
                        **(
                            {"row_selector": row_selector}
                            if isinstance(row_selector, dict) and row_selector
                            else {}
                        ),
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
            "chart_trace_refs": self._dedupe_refs(
                chart_refs,
                ("chart_index", "value_field", "point_selector"),
            ),
            "table_trace_refs": self._dedupe_refs(
                table_refs,
                ("table_index", "column_key", "row_selector"),
            ),
            "report_trace_refs": self._dedupe_refs(report_refs, ("start", "end")),
            "query_snapshots": snapshot.query_snapshots,
            "diagnostics": {
                "code_hash": snapshot.code_hash,
                "result_hash": snapshot.result_hash,
                "lineage_operation_count": len(lineage_index["operation_nodes"]),
                "lineage_output_count": len(lineage_index["output_nodes"]),
                "compiled_output_node_ids": sorted(output_claims),
                "resolver_unresolved_outputs": [
                    {
                        "output_node_id": str(item.output_node_id or "").strip(),
                        "source_anchor": (
                            item.source_anchor.model_dump()
                            if item.source_anchor is not None
                            else None
                        ),
                        "reason": str(item.reason or "").strip(),
                    }
                    for item in plan.unresolved_outputs
                ],
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
            signature = tuple(
                _stable_json(value)
                if isinstance(value := ref.get(key), (dict, list, tuple, set))
                else value
                for key in keys
            )
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
            "status": "partial" if (
                lineage_index.get("output_nodes")
                or lineage_index.get("report_source_anchors")
            ) else "failed",
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
