# query.local_file

## 文件 API

<!-- runtime-helper-reference -->

- 文件 helper 已直接注入代码执行命名空间，直接调用，不能从 `query.local_file`、`runtime_api` import；契约标识不是 Python 包。
- 组合 helper 从本轮快照用选定的 `datasource_id` 解析路径，不要抄写或编造路径；旧代码可传 `file_path`。不使用 `load_data_with_sql`。
- 普通文件优先使用 `query_local_file(...)` 一次完成校验、快照和加载。
- 无筛选完整明细可省略 `aggregation_hints`；有筛选或非明细聚合时，需提供与实际 DataFrame 实现一致的 `filter_expressions` / `aggregation_hints`。
- 数据源推荐低内存读取或全量读取发生内存错误时，优先使用 `query_local_file_low_memory(...)`；它返回批次迭代器，必须在每批内过滤和累计，禁止把全部批次重新拼成超大 DataFrame。
- 特殊控制流才拆开 `validate_query_constraints(...)` 和底层 loader。
- Excel 带 `sheet_name` 时必须传入。同一工作簿的多个工作表可共享路径，但用 `datasource_id` 和 `sheet_name` 区分。
- `usecols/dtype/parse_dates` 仅供低内存读取；字段必须来自当前文件完整 Schema。
- 文件 Probe 用推荐 loader，只累计总行数、时间范围和 Top 候选。
- 多文件先分别分析；关系已确认时，先按各自粒度过滤/聚合再关联。

## 低内存与完整范围

- `recommended_loader=load_local_file` 时普通读取返回完整 DataFrame；`recommended_loader=load_local_file_low_memory` 时首轮即按批次迭代，不能把迭代器直接 `.groupby()`、取列或制图。
- 低内存读取优先传 `usecols/dtype/parse_dates`，在每批内完成过滤、聚合、TopN、时间覆盖和有限明细累计；禁止把全部批次 `concat` 回完整大表。
- 如果上一执行已发生 `MemoryError`、OOM 或子进程资源耗尽，即使推荐值缺失，修复也必须切换低内存策略，不能重复整表读取。
- CSV、xlsx、xlsm 可按 helper 能力批处理；旧版 xls 仅 best-effort，无法安全读取时明确为路径能力受限，不反复 OOM 重试。

## 过滤与校验

- 正式文件分析优先由 `analysis_parameters` 构造 `file_filters`，再交给组合查询 API；校验通过后 loader 会对 DataFrame 实际执行这些结构化过滤，并把它们写入 QuerySnapshot。
- Helper 返回过滤后的明细，不分组聚合；查询 `aggregation` 为 `None` 或 `detail`，分组在后续计算中完成。
- `file_filters` 是 `list[dict]`，每项为 `{field, op, value}`；支持 `eq`、`in`、`gt`、`gte`、`lt`、`lte`、`is_null`、`not_null`。字段必须来自当前文件 Schema。`is_null` / `not_null` 不传 `value`。
- `filter_expressions` 是 `list[str]`，仅校验复杂派生筛选，不会被 loader 执行；读取后必须应用同一表达式，不能放结构化筛选对象。
- 最小调用与执行关系如下：

```python
data = query_local_file(
    query_key="q_file_detail",
    query_constraints={"time_range": None, "target_filters": [],
                       "aggregation": {"grain": "detail", "metric": "detail"}},
    datasource_id=123,  # 替换为本轮选定数据源的真实 ID
)
```

返回 DataFrame；筛选时添加 `file_filters` 并同步口径。校验失败自动反馈。

```python
file_filters = [{"field": "category", "op": "eq", "value": category}]
data = query_local_file(
    query_key="q_category_detail",
    query_constraints={"time_range": None, "target_filters": [
        {"semantic_role": "business_category", "field_hint": "category",
         "value": str(category), "match_mode": "exact"}], "aggregation": None},
    datasource_id=123,  # 替换为本轮选定数据源的真实 ID
    file_filters=file_filters,
)
```

- 精确匹配使用 `==/.eq/.isin`。`str.contains`、正则和候选集合用于 Probe 发现真实值，不能未经收敛直接作为正式统计口径。
- 月粒度字符串字段优先使用 `file_filters=[{"field": "month", "op": "eq", "value": "2026-03"}]`；复杂月份派生表达才使用带真实字面量的 `filter_expressions`，不能只写校验器无法解析的变量名。
- 大文件 Probe 可以流式遍历以累计总行数、时间覆盖、命中数和 Top 分布，但不保存样本行作为正式结果。

<!-- runtime-prompt-omit-start -->
## 完整组合示例

下面示例只说明文件查询约束、真实过滤和最终产物的闭环。数据源 ID 与字段必须替换为当前选择和文件 Schema 中的真实值。

<!-- canonical-runtime-example: local_file -->
```python
execution_intent = "analysis"

analysis_parameters_schema = {
    "parameters": [
        {
            "name": "target_categories",
            "type": "multi_select",
            "label": "目标类别",
            "required": True,
            "options": ["类别A", "类别B"],
        }
    ],
    "defaults": {"target_categories": ["类别A", "类别B"]},
}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema["defaults"])
target_categories = [str(item) for item in analysis_parameters["target_categories"]]

query_constraints = {
    "time_range": None,
    "target_filters": [{
        "semantic_role": "primary_entity",
        "field_hint": "category",
        "value": target_categories,
        "match_mode": "exact",
    }],
    "aggregation": {"grain": "detail", "metric": "detail"},
    "notes": "读取用户选择类别的明细，随后在 DataFrame 中按类别汇总。",
}
file_filters = [{"field": "category", "op": "in", "value": target_categories}]
data = query_local_file(
    query_key="q_local_category_detail",
    query_constraints=query_constraints,
    datasource_id=123,  # 替换为本轮选定数据源的真实 ID
    file_filters=file_filters,
)
filtered = data.copy()
if filtered.empty:
    raise_no_data_error("文件读取成功，但目标类别范围内没有记录。")

summary = (
    filtered.groupby("category", as_index=False)["metric_value"]
    .sum()
    .sort_values("metric_value", ascending=False)
    .reset_index(drop=True)
)
total_value = float(summary["metric_value"].sum())
top_row = summary.iloc[0]
leader_rows = select_extreme_rows(summary, "metric_value", mode="max")
if len(summary) == 1:
    comparison = "当前只有一个类别，尚不能比较类别差异。"
elif len(leader_rows) > 1:
    comparison = f"{'、'.join(leader_rows['category'].astype(str))}并列最高，当前数值不能区分它们的领先次序。"
else:
    second_row = summary.iloc[1]
    gap = float(top_row["metric_value"] - second_row["metric_value"])
    comparison = f"{top_row['category']}比第二名{second_row['category']}高 {gap:,.2f}；该差额是后续核查明细构成的依据，不直接证明原因。"
charts = [build_chart_result(
    chart_kind="bar",
    data=summary,
    title="目标类别核心指标对比",
    category_field="category",
    value_field="metric_value",
    sort_field="metric_value",
    sort_order="desc",
)]
tables = [build_table_result(
    title="类别汇总",
    columns=[
        {"key": "category", "label": "业务类别"},
        {"key": "metric_value", "label": "核心指标"},
    ],
    data=summary,
    value_precision=2,
)]
analysis_report = "\n\n".join([
    "## 核心结论",
    f"目标类别核心指标合计为 {total_value:,.2f}，"
    f"最高类别值为 {top_row['metric_value']:,.2f}。",
    "## 类别比较与判断边界",
    comparison,
])
result = save_analysis_result(
    analysis_report=analysis_report,
    charts=charts,
    tables=tables,
    analysis_parameters_schema=analysis_parameters_schema,
    analysis_parameters=analysis_parameters,
)
```
<!-- runtime-prompt-omit-end -->
