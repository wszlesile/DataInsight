# query.relational

## 查询 API

<!-- runtime-helper-reference -->

## 访问与字段

- 关系型 `table` 的常规查询使用 `query_data_with_sql(...)`；需要特殊控制流时可使用底层 `load_data_with_sql(...)`。
- FROM/JOIN 必须使用当前来源的 `query_contract.relation_expression`。`uns_get` 来源不得改用 `datasource_identifier`、底层 alias 或 `fqe_uns.public.<alias>`；物理表使用其自身表达式，时序来源遵守时序协议。下例是 `uns_get` 场景。
- 字段来自同一来源的 `query_contract.allowed_fields`；若提供 `allowed_fields_ref=metadata_schema.properties`，则以该来源完整 properties 的键集合为准。`datasource_name` 不是 SQL 表名，相邻数据源同名字段也不能串用。
- 遵守 `query_contract.sql_dialect` 和 `identifier_quote`：查询先经过 FedQuery 外层解析，底层数据源引擎不决定外层 SQL 的标识符语法。需要引用的列、表和别名使用双引号，并将名称内部的双引号加倍；字符串值使用单引号或绑定参数。优先在 SQL 层完成字段裁剪、业务时间过滤、JOIN、聚合、排序和 TopN，再用 pandas 整理小结果。
- 同时遵守 `query_contract.operation_capabilities`。能力标为 `unsupported` 的运算不能换用另一数据库方言函数；应改写为当前查询层支持的分组结果，或在结果有界时于 pandas 组合。
- Loader 自动恢复 Schema 中直接投影的数值字段和明确聚合别名。表达式派生数值列用 `numeric_columns=[...]`；合同号、设备号、产品编码等业务标识用 `text_columns=[...]` 保真，不能数值化。
- SQL 派生列别名使用当前方言的非保留标识（如 `row_count`、`record_count`）；不把 `rows`、`order`、`group` 等保留字直接作为未引用别名。`numeric_columns`/`text_columns` 与实际别名保持一致。
- Schema 关系声明 `key_normalization=integral_numeric_text` 时，两侧键都作为文本加载，再分别调用 `normalize_relationship_key(series)` 后关联；不要自行通过浮点或 `astype(str)` 猜测转换。

## 范围与容量

- 单次 SQL 结果最多 `runtime_capabilities.query_execution.max_rows_per_sql_result` 行。这是每次物理查询容量，不是总体分析范围。
- 总体数量、分布、趋势、排名和根因统计优先下推最终过滤、JOIN 与分组聚合。确实需要完整明细时，使用稳定、唯一排序键分页并在每页增量累计；不能把全部分页重新拼成超大 DataFrame。
- `LIMIT` 仅用于用户要求的 TopN、有限明细、采样或有明确披露的展示范围；不能用任意截断计算总体指标。
- “最近 N 条”先按业务时间确定顺序后取最近值，不能用 `head(N)` 却声称是最近数据。
- 查询成功空集才属于无数据；SQL 语法、认证、表或字段错误属于路径不可用并进入修复。

## 筛选与时间

- 用户明确的年份、月份、对象、型号、编号、状态和粒度必须进入 `analysis_parameters`、`query_constraints` 和 SQL，不能遗漏或无依据放宽。
- `match_mode=exact` 使用等值/集合精确匹配。只有 Probe 证明库内表达存在轻微差异且业务含义一致后，才把收敛后的真实值用于正式查询；不能静默改成 LIKE/contains。
- 对象名中的空格、连字符、斜杠、括号和 `Φ/φ` 默认属于完整值，不自行拆词。
- 时间范围采用半开区间 `>= start AND < end`，优先过滤原始业务日期/时间字段。派生 year/month/day 只用于分组或展示。
- 时间分桶优先分别返回数值 year/month/day，再在 pandas 构造展示标签。不要使用可能被联邦层解释为布尔运算的 `||`；确需拼接时使用当前方言明确支持的 `CONCAT(...)`。

## JOIN 与聚合安全

- 多表联合优先先过滤、在各自事实粒度预聚合，再按 Schema 中 `confirmed` 关系 JOIN。字段同名不等于存在业务关系。
- `aggregation_safety=unsafe_repeated_by_join` 一侧的金额、时长、数量不能在一对多展开后直接求和；先在原事实粒度聚合、按业务键去重，或使用明确分摊规则。
- 主体覆盖率分子是主体键集合与辅助键集合交集的去重数，分母是主体人口键去重数；辅助孤立键和一对多明细行不进入主体数量。
- 不同业务对象、分母、粒度和单位的计数不能直接相加或排序。跨来源比较前先统一总体、时间、粒度和单位；无法统一时分别呈现。
- FedQuery CTE 可用于主查询 `FROM/JOIN`，不要在标量子查询重复引用当前 CTE。多个单值聚合使用聚合 CTE 加 `CROSS JOIN` 或派生表组合。
- 高成本跨表 JOIN 超时时，先修正过滤、连接顺序和预聚合；若关系或能力确实无法支持，保留可回答的独立事实并说明边界，不能静默改成另一指标。

<!-- runtime-prompt-omit-start -->
## 完整组合示例

下面示例只说明一份正式代码如何把查询、计算、图表、表格和判断性报告组合成闭环。实际代码必须把示例表名和字段全部替换为当前完整 Schema 中的真实标识，不能照抄占位名称。

<!-- canonical-runtime-example: relational -->
```python
execution_intent = "analysis"

analysis_parameters_schema = {
    "parameters": [],
    "defaults": {},
}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema["defaults"])

query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "category", "metric": "metric_value"},
    "notes": "按业务类别汇总核心指标并比较结构。",
}
sql = """
SELECT category, SUM(metric_value) AS metric_value
FROM uns_get('业务域/已选节点') AS selected_source
GROUP BY category
"""
summary = query_data_with_sql(
    query_key="q_category_metric_summary",
    query_constraints=query_constraints,
    sql=sql,
    aggregation_hints=["category", "metric_value", "sum"],
    numeric_columns=["metric_value"],
    text_columns=["category"],
)
if summary.empty:
    raise_no_data_error("当前业务范围内没有可分析记录。")

ranked = summary.sort_values("metric_value", ascending=False).reset_index(drop=True)
total_value = float(ranked["metric_value"].sum())
top_row = ranked.iloc[0]
top_value = float(top_row["metric_value"])
leader_names = ranked.loc[ranked["metric_value"] == top_value, "category"].astype(str).tolist()
share_is_meaningful = bool((ranked["metric_value"] >= 0).all() and total_value > 0)
top_share = top_value / total_value if share_is_meaningful else None

charts = [build_chart_result(
    chart_kind="bar",
    data=ranked,
    title="各类别核心指标对比",
    category_field="category",
    value_field="metric_value",
    sort_field="metric_value",
    sort_order="desc",
    value_precision=2,
)]
tables = [build_table_result(
    title="类别汇总",
    columns=[
        {"key": "category", "label": "业务类别"},
        {"key": "metric_value", "label": "核心指标"},
    ],
    data=ranked,
    value_precision=2,
)]

if len(ranked) == 1:
    comparison_finding = "当前范围只有一个类别，不能进行类别间比较。"
    next_step = "建议补充可比较的期间或类别，再判断结构变化。"
elif len(leader_names) > 1:
    comparison_finding = f"{'、'.join(leader_names)}并列最高，每类为 {top_value:,.2f}。"
    next_step = "建议对比并列类别的明细构成和后续变化，当前汇总尚不能区分其变化原因。"
else:
    second = ranked.iloc[1]
    lead_gap = top_value - float(second["metric_value"])
    comparison_finding = (
        f"{top_row['category']}最高，为 {top_value:,.2f}；"
        f"比第二名 {second['category']}高 {lead_gap:,.2f}。"
    )
    next_step = "建议优先拆解前两类的差额来源；当前排序说明规模差异，尚不能解释形成原因。"
share_finding = (
    f"单个最高类别占合计的 {top_share:.1%}。"
    if top_share is not None
    else "合计为零或类别值含负数，不将其解释为贡献份额。"
)
analysis_report = "\n\n".join([
    "## 核心结论",
    f"本轮核心指标合计为 {total_value:,.2f}。{comparison_finding}",
    "## 关键数据发现",
    share_finding,
    "## 业务建议",
    next_step,
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

- `schema_scope=declared_business_fields` 表示 UNS 定义提供业务字段，不保证枚举查询入口的全部物理列。缺少列声明不等于物理表不存在时间列或平台标识列。只根据已声明/已观测字段统计时明确该字段范围；同值重复不能称为物理完整行重复，未验证完整结构不能断言“无时间字段”。

- `reference_target` 是平台给出的 UNS 引用目标身份，与业务表之间的 JOIN/relationships 不同；relationships 为空不能解释为引用节点没有引用目标。访问失败保留已知引用身份，报告实际失败原因，不将身份缺失或查询失败解释成数据为零。
