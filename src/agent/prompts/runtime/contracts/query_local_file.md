# query.local_file

## 文件 API

<!-- runtime-helper-reference -->

- 本地文件使用 `datasource_identifier` 作为路径，不使用 `load_data_with_sql`。
- 普通文件优先使用 `query_local_file(...)`，由后端一次完成约束校验、QuerySnapshot 注册和 `load_local_file(...)` 加载。
- 当查询范围是完整文件、没有时间或对象筛选且粒度为 `detail` 时，组合 helper 会直接确认全量明细读取；不需要为了通过校验重复填写 `aggregation_hints=["detail"]`。存在筛选或非明细聚合时，仍须提供与真实 DataFrame 实现一致的 `filter_expressions` / `aggregation_hints`。
- 数据源推荐低内存读取或全量读取发生内存错误时，优先使用 `query_local_file_low_memory(...)`；它返回批次迭代器，必须在每批内过滤和累计，禁止把全部批次重新拼成超大 DataFrame。
- `validate_query_constraints(...)`、`load_local_file(...)` 和 `load_local_file_low_memory(...)` 继续用于需要拆分校验、派生范围或特殊控制流的场景。
- Excel 带 `sheet_name` 时必须传入。同一工作簿的多个工作表可共享路径，但用 `datasource_id` 和 `sheet_name` 区分。
- `usecols/dtype/parse_dates` 只声明实际需要的字段，字段严格来自当前文件完整 Schema。
- 文件 Probe 遵守推荐 loader，只累计行数、时间范围、Top 候选等轻量观察。
- 多文件业务独立则分别分析；存在已确认关系时先在各自粒度过滤/聚合，再做受控关联。

## 低内存与完整范围

- `recommended_loader=load_local_file` 时普通读取返回完整 DataFrame；`recommended_loader=load_local_file_low_memory` 时首轮即按批次迭代，不能把迭代器直接 `.groupby()`、取列或制图。
- 低内存读取优先传 `usecols/dtype/parse_dates`，在每批内完成过滤、聚合、TopN、时间覆盖和有限明细累计；禁止把全部批次 `concat` 回完整大表。
- 如果上一执行已发生 `MemoryError`、OOM 或子进程资源耗尽，即使推荐值缺失，修复也必须切换低内存策略，不能重复整表读取。
- CSV、xlsx、xlsm 可按 helper 能力批处理；旧版 xls 仅 best-effort，无法安全读取时明确为路径能力受限，不反复 OOM 重试。

## 过滤与校验

- 正式文件分析先由 `analysis_parameters` 构造真实 `filter_expressions`，再交给组合查询 API；校验通过后才会加载文件，代码随后对 DataFrame 应用同一过滤。
- 精确匹配使用 `==/.eq/.isin`。`str.contains`、正则和候选集合用于 Probe 发现真实值，不能未经收敛直接作为正式统计口径。
- 月粒度字符串字段可用 `>= '2026-03' AND < '2026-04'` 或 `== '2026-03'` 的等价 DataFrame 表达；`filter_expressions` 必须带真实字面量，不能只写校验器无法解析的变量名。
- 大文件 Probe 可以流式遍历以累计总行数、时间覆盖、命中数和 Top 分布，但不保存样本行作为正式结果。

<!-- runtime-prompt-omit-start -->
## 完整组合示例

下面示例只说明文件查询约束、真实过滤和最终产物的闭环。路径与字段必须替换为当前文件 Schema 中的真实值。

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
    "aggregation": {"grain": "category", "metric": "metric_value"},
    "notes": "按用户选择的类别汇总文件中的核心指标。",
}
filter_expressions = [f"data['category'].isin({target_categories!r})"]
data = query_local_file(
    query_key="q_local_category_summary",
    query_constraints=query_constraints,
    file_path=r"D:\\path\\selected_file.csv",
    filter_expressions=filter_expressions,
    aggregation_hints=["category", "metric_value", "sum"],
)
filtered = data[data["category"].astype(str).isin(target_categories)].copy()
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
    f"其中 {top_row['category']} 排名第一（{top_row['metric_value']:,.2f}）。",
    "## 业务建议",
    "建议结合头部类别的明细构成和后续变化判断其贡献是否可持续。",
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
