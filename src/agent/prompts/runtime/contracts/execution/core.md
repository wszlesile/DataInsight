# execution.core

## Python 执行闭环

- `probe_analysis_data` 和 `submit_analysis_program` 分别确定探测与正式执行用途，代码无需重复声明 `execution_intent`；后端会在保存和执行完整源码前补齐标识。显式保留标识时必须与调用工具一致。
- `probe_analysis_data` 与 `submit_analysis_program` 都是独立执行单元，不继承上次调用的 DataFrame、变量或 imports。
- `analysis` 必须在一份代码内完成参数读取、查询约束校验、数据访问、计算、空数据判断、当前结果协议要求的输出构造和一次结果保存。
- `probe` 只做有界真实观察，不计算正式指标，不创建正式产物，不保存半成品报告。`probe_analysis_data` 会自动返回查询执行事实和代码打印的有界摘要；需要给观察命名或组织更丰富的诊断时，可用 `result = request_retry(retry_type="probe_feedback", diagnostics=...)` 自定义反馈。
- Probe 反馈中的 `_query_execution_outcomes` 由后端按真实查询快照提供执行状态和错误；`failed` 不表示无数据。结合对应查询与模型记录的观察判断后续动作。
- 需要查看行数、样例、枚举、格式或覆盖范围时，调用 `probe_analysis_data`，返回结构化观察。
- 已具备正式分析所需事实时，调用 `submit_analysis_program`，一次提交完整 Python 分析程序，不分段提交数据加载、中间 DataFrame 或报告草稿。
- 代码只读，不访问网络、进程或系统敏感资源，不硬编码凭据，不修改数据源。

## Python 执行环境

`probe_analysis_data` 和 `submit_analysis_program` 是提交 Python 源码的外部工具，不是代码内可导入的 Python 模块。代码在后端提供的执行命名空间中运行。

`pd` 和以下函数已注入执行命名空间，可直接使用，不要重新定义同名函数。数据访问与产物函数列在对应协议中。签名中的 `*` 表示其后参数须使用关键字传入。

## Runtime API

<!-- runtime-helper-reference -->

`build_chart_document` 和 `save_empty_analysis_result` 是后端兼容/底层 helper，不作为模型生成正式代码的首选接口。常规无数据路径使用 `raise_no_data_error(...)`。

### Helper 语义

- `get_day_range(days_ago, timezone_name)` 返回业务时区内某个自然日的零点和最后一微秒（包含式终点）；`0/1/2` 分别表示今天/昨天/前天。需要 SQL 左闭右开范围时，使用起点加一天作为排他终点，不把包含式终点误称为次日零点。近 N 天等相对范围由运行时参数和当前触发时间派生，不固化任务创建时刻。
- `probe_distinct_values(...)` 返回字段高频真实取值；`probe_text_candidates(...)` 只提供规范化和相似度候选。候选必须有真实观察支撑，不能仅凭相似字面替换业务枚举。
- `describe_time_coverage(...)` 返回时间列的真实覆盖范围，用于判断相对日期未命中究竟是范围偏移还是确实空集。
- `normalize_relationship_key(...)` 只规范跨源连接键的数据类型和空白，不改变业务值，不把不同编码体系强行映射。
- `get_analysis_parameters(defaults)` 在首次分析返回默认值，在本地重跑或洞察任务执行时合并本次参数覆盖值。它保留值和类型，不解析或改写时间，不自动生成展示终点等派生字段。可编辑参数及默认值在参数 Schema 中定义。
- `build_markdown_table(...)` 生成报告内 Markdown 表格；业务表头使用 `{"key":"真实字段","label":"业务名称"}`，不向用户暴露变量名。
- `raise_no_data_error(...)` 表示数据访问成功但业务范围为空，会触发上层 Probe/纠偏；表不存在、路径不可访问、认证或查询错误不属于无数据。

## 查询结果与计算语义

- 查询后先检查真实列、字段类型、分组粒度和排序键，再计算总量、同比环比、TopN 和产物。这适用于 SQL、文件及其他来源的结果，不由输出格式决定。
- 多维聚合结果继续显式保留全部维度；没有业务依据不能把多维结果直接当成更粗粒度单序列或总量。需要进一步汇总时，先确认字段的可加性和目标总体。
- 排序使用原始时间、数值或等级键，再生成展示标签，不能只按格式化展示文本排序。“最近 N 条”先确定时间顺序后取最近值，不能用 `head(N)` 却声称是最近数据。
- 占比和对应数量描述同一筛选群体，分子复用同一数量或筛选结果；明确区分比例、百分点差和相对增幅。相关性等统计判断保留有效样本量、可比较范围和适用条件，样本不足时只作为线索，不认定因果。
- 同比、环比和基期比较使用可比窗口。`analysis_semantic_context.analysis_as_of_date` 是分析基准日，不保证数据已完整覆盖到该日。当前年、季度或月份尚未结束时，在正式查询中取得业务时间字段的实际最大日期或最大已完成周期，将基期对齐同一月日或同一已完成周期；不拿本期部分累计与基期全年直接计算增长率。全年基期可以作为明确区分的背景值。
- 查询实际截止日期可以在同一份正式程序内完成；仅在查询为空、格式异常或仍有影响判断的技术未知时再 Probe，不为已能确定的日期额外探测。

## 可复跑分析参数

正式分析在任何数据加载和查询校验前定义参数 Schema：

```python
analysis_parameters_schema = {
    "parameters": [
        {
            "name": "stable_english_key",
            "type": "string",
            "label": "用户可读名称",
            "required": True,
            "description": "该参数控制的查询口径",
            "options": ["可选值1", "可选值2"],
        }
    ],
    "defaults": {"stable_english_key": "可选值1"},
}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema["defaults"])
```

- `name` 使用稳定英文 key；`defaults` 覆盖全部必填参数并与当前问题一致。
- 类型支持 `string/number/integer/boolean/date/datetime/time_range/select/multi_select`。有权威枚举时 `select/multi_select` 提供 `options`；没有可靠枚举时不编造列表。
- `time_range` 使用 `{"start":"...","end":"...","end_exclusive":true}`。相对时间任务保留可复算语义，不能把“近十分钟”固化成创建任务时的绝对时间。
- 参数只描述以后可以改变的查询口径，包括时间、对象、筛选、粒度、TopN、采样窗口和统计方法；不放颜色、布局或报告措辞。
- 后续 `query_constraints`、SQL、DataFrame 过滤、分组、排序、标题、口径说明均读取 `analysis_parameters`，不能硬编码第二套值。

## 查询约束

每条物理查询使用独立稳定的 `query_key`。常规 SQL 交给 `query_data_with_sql(...)` 固定执行校验、注册、加载顺序；使用底层 API 时，必须先调用 `validate_query_constraints(...)`，返回 `None` 后才能执行 loader。

```python
query_constraints = {
    "time_range": None,
    # 或 {"start":"2026-03-01","end":"2026-04-01","end_exclusive":True}
    "target_filters": [
        {
            "semantic_role": "primary_entity",
            "field_hint": "field_name",
            "value": "value-or-list",
            "match_mode": "exact",
        }
    ],
    "aggregation": {"grain": "month", "metric": "sales_amount"},
    "notes": "一句话口径摘要",
}
```

- 无时间范围写 `None`，无筛选写 `[]`，无聚合写 `None`。不要写 `{"start":None,"end":None}`。
- `target_filters[*].value` 是非空字符串或非空字符串列表；状态也用字符串。`match_mode` 仅为 `exact/fuzzy`，SQL LIKE 和 pandas contains 都属于 fuzzy。
- `time_range` 表示该查询覆盖的分析期间；`aggregation.grain` 只表示该物理查询实际返回的行粒度，不能把“本周/本月”等分析期间写成 `week/month`，除非 SQL 或 DataFrame 确实按周/月分组。
- `aggregation.grain/metric` 使用该物理查询真实实现的业务粒度和指标字符串；明细查询或不承担聚合的辅助查询将 `aggregation` 写为 `None`。一份分析包含不同粒度的查询时，为每个 `query_key` 构造对应的 query constraints，不能把同一个 aggregation 声明复用于所有查询；`notes` 不能代替结构字段。
- SQL、参数、文件过滤和约束表达同一范围。参数化 SQL 把 loader 的同一参数传入 `query_params`；文件/pandas 把真实字面量表达式传入 `filter_expressions`。
- 关系型业务日期范围直接过滤原始时间字段；派生年月只用于分组和展示。
- 时序内部 SQL 或混合 SQL 各片段放入 `sql_context.fragments`，声明 `kind`、backend、`inner_sql/sql`、`datasource_identifier` 及字段元数据；循环查询多个来源时每个来源都有片段。
- 复用另一查询已验证的主体总体时使用 `scope_source_query_key` 和 `scope_binding` 显式声明，不能靠变量名暗示继承，也不要给子表强加语义不等价的时间字段。
- 函数和循环中同样使用组合查询 API；每条业务查询使用稳定、可区分的 query_key，保持校验、快照和真实执行对应。不能用宽泛异常捕获吞掉契约错误或将查询失败解释为零值、空集。

## 标准 analysis 骨架

关系型与时序 SQL 的常规路径优先使用 `query_data_with_sql(...)`。它在后端按固定顺序完成约束校验、QuerySnapshot 注册和物理查询；校验失败会自动返回结构化 Retry，不需要手写 `if validation is not None` 分支。复杂的分阶段查询仍可使用底层 `validate_query_constraints(...) + load_data_with_sql(...)`。

```python
execution_intent = "analysis"

analysis_parameters_schema = {"parameters": [...], "defaults": {...}}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema["defaults"])

primary_query_scope = {
    "time_range": None,
    "target_filters": [],
}

query_constraints = {
    **primary_query_scope,
    "aggregation": None,
    "notes": "当前物理查询的口径",
}

# 先由 analysis_parameters 构造 sql/sql_params 和可选的 sql_context
data = query_data_with_sql(
    query_key="stable_query_key",
    query_constraints=query_constraints,
    sql=sql,
    params=sql_params,
    sql_context=sql_context,
    aggregation_hints=[...],
)
if data is None or data.empty:
    raise_no_data_error("当前查询口径内无数据", ["必要的结构化范围说明"])

# 基于真实数据计算事实，再严格按本轮 result.* contract pack
# 构造 save_analysis_result(...) 所需的唯一终态参数。
```

父查询先确定主体集合、子查询再补充关联事实时，子查询继承父查询已经验证的分析范围。关联值必须作为 `%s` 参数传入，不能拼成 SQL 字面量，也不能重复伪造一套子表时间口径：

<!-- runtime-behavior-example: parent-detail-scope -->
```python
batch_ids = parent_data["batch_id"].dropna().astype(str).unique().tolist()
if not batch_ids:
    raise_no_data_error("父查询范围内没有可关联的业务对象。")
placeholders = ", ".join(["%s"] * len(batch_ids))
child_sql = f"SELECT batch_id, event_type FROM child_events WHERE batch_id IN ({placeholders})"
child_data = query_data_with_sql(
    query_key="q_batch_events",
    query_constraints={
        "time_range": None,
        "target_filters": [],
        "aggregation": None,
        "notes": "按父查询批次集合继承范围并补充事件事实",
    },
    sql=child_sql,
    params=batch_ids,
    scope_source_query_key="q_risk_batches",
    scope_binding={"source_field": "batch_id", "target_field": "batch_id"},
)
```

查询范围声明属于具体物理查询，不是给所有查询机械展开的全局模板。产品主数据、字典等不受分析时间约束的补充查询应使用 `time_range=None`、`target_filters=[]`；若只需父查询命中的实体，再通过 `scope_source_query_key` 继承范围。只有子查询拥有独立且业务等价的时间语义时，才在子查询中直接实现 `time_range`。

本地文件用同一顺序：简单字段范围优先传可执行的 `file_filters=[{"field": ..., "op": ..., "value": ...}]` 给 `query_local_file(...)`，由 loader 真正过滤并记录到 QuerySnapshot；只有复杂派生过滤才使用 `filter_expressions`，且代码必须在读取后实际执行同一表达式。不能把仅用于校验的 `filter_expressions` 当作已生效的物理筛选。

## Probe 与无数据

- Probe 用于确认枚举、存储格式、时间覆盖、查询路径、字段归属或过滤为何为空。可使用 `probe_data_with_sql`、`probe_distinct_values`、`probe_text_candidates`、`describe_time_coverage` 或目标文件 loader 的有界读取。
- 多条候选查询必须在同一次 Probe 中逐项检查，不因第一条成功而提前结束。`probe_data_with_sql` 的 `success + row_count>0`、`success/no_data + row_count=0`、`query_error` 分别对应可分析、成功空集和路径不可用。
- diagnostics 返回真实观察，包括逐路径 datasource、status、row_count、columns、时间覆盖、候选值或错误类型；不能只返回 SQL、计划或推测。
- 探测可以确认空格、大小写、连接符、日期边界和真实枚举；只有观察证明语义一致时才纠偏。Probe 中的模糊候选不能直接变成正式统计口径。
- 全部必要路径成功查询但为空且原因尚待核实时，调用 `raise_no_data_error(...)`，不能生成全零报告、空排名或伪图表。依据真实观察确认查询口径仍为空后，使用当前输出协议说明无数据原因；只有用户要求或观察到的原因支持明确后续行动时才给建议，不需要为了收口重复探测到次数上限。
- 聚合查询即使返回一行，代表目标业务总体的 `COUNT` 为 0 时仍没有可分析人群，不能仅以 DataFrame 非空判断有数据。某个金额、比率或其他指标为 0 不等于总体为空；多路径仍须分别判断，不能因此丢弃其他非空路径。
- 部分路径为空时保留成功路径并披露受影响结论；`no_data` 与 `data_source_unavailable/query_error` 分开说明。
- 文件不存在、表不存在、接口不可访问和认证失败不是无数据，应返回相应结构化错误。

Probe 收口示例：

```python
execution_intent = "probe"
result = request_retry(
    retry_type="probe_feedback",
    message="已完成必要的真实口径探测。",
    diagnostics={"query_observations": probe_observations},
    repair_instructions=[
        "切换为 execution_intent='analysis'。",
        "正式分析重新查询并计算所需路径；已有观察用于识别范围、空集和不可用原因，未探测路径不等于不可分析。",
    ],
)
```

## 保存、修复与安全

- 正式分析只调用一次 `result = save_analysis_result(...)`，并且只使用本轮加载的 `result.*` contract pack 所规定的终态参数。
- 外层执行工具参数 `used_datasource_ids` 与代码真实访问范围一致，由后端记录来源；Python 内的 `save_analysis_result` 只接收当前结果协议的保存参数。
- 局部技术修复只有在反馈明确给出 `repair_target` 时，才可使用 `code=""`、`base_execution_id + patches`；每个 patch 必须完整替换一个授权 Python 语句或代码块，并继续交付该语句的输出。查询范围、字段归属、JOIN、聚合、计算或产物结构需要超出授权范围调整时，必须在当前完整洞察语义下提交完整 `code`。
- 禁止 `except Exception: pass`，也不能把数据访问、字段、类型、图表或保存错误改写成空 DataFrame、空列表或“无数据”。
- 维度键缺失不等于数值为零。先统一键类型并检查存在性，不能用 `mapping.get(key, 0)` 把口径缺失伪装成真实零值。
- 最终结果来自工具执行，不把 Python 代码、工具参数、内部错误或调用过程作为用户答复。
