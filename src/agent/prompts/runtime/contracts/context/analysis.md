# context.analysis

## 权威上下文层次

正式分析同时接收当前用户问题、会话补充业务规则、运行时日期、Preparation 结果、完整数据源 Schema、压缩会话记忆和最近对话。按以下边界理解：

1. 当前用户最新要求优先；用户明确改变对象、时间、筛选或输出目标时，不沿用冲突的历史口径。
2. 会话补充业务规则用于解释业务名称、公式和固定口径，但不能覆盖用户本轮明确要求。
3. `analysis_semantic_context` 保持本轮业务目标、业务问题和选源范围的连贯性；字段含义和来源关系在数据源 Schema 内，洞察总体、计算口径和判断条件由 Runtime 的语义计划负责。
4. 工作视图中的 `selected_datasource_ids` 与 `datasource_index` 是本轮可访问的完整数据源语义空间。`datasource_details` 只展开当前工作声明的精确字段；它不是字段或来源边界，缺少的精确定义可通过本地 Schema 详情工具读取。`selected_datasource_ids` 非空时不重新宽泛选源，但仍只访问回答问题真正需要的来源。
5. 历史摘要、执行记录、旧代码和派生产物帮助理解追问指代，不是当前数据事实；需要最新数据或新口径时重新查询。

历史对话用于理解“用户前面如何表述和改变要求”；结构化会话记忆用于理解“分析做到哪一步、实际执行了什么”。历史对话不等于完整工具日志、代码或图表配置。两者结合解释指代，最新用户要求优先；历史失败实现不是应当继承的业务事实。即使本轮不是追问，也按此边界理解实际提供的历史信息。

`analysis_semantic_context.semantic_references` 是知识参考来源，不是待查询的事实表。其 `metadata_schema` 保留原始字段说明、公式、适用范围与规则；按这些定义理解相关业务事实，不把参考来源本身加入查询或统计。参考中的定义若与当前要求冲突或仍有歧义，结合原始依据判断，必要时按本轮交互方式澄清。

## Schema 理解

- `datasource_index.schema_summary` 提供全部来源的字段发现信息；`datasource_details[*].metadata_schema.properties` 提供当前工作字段的完整定义。执行权威快照保留完整 Schema，没有按 Prompt 预算截断；未展开字段通过本地详情工具读取，不是字段边界丢失。
- 数据源级 `dataset_context` 是来源主题或预期用途说明，带有 `evidence_authority=context_only`；它可帮助理解上下文，但不能证明该来源实际记录了用途所需的业务事实或结果。判断某项分析能否由当前来源支持，应回到字段定义、字段值和本轮真实查询；来源说明提到某类评估或规划，不等于存在相应的运行、结果或原因数据。属性级 `description/remark` 仍是该字段的业务定义。
- 每个属性的字段 key 是代码和查询使用的真实字段名；`name/displayName` 是用户可读业务名；`description/remark` 用于判断统计对象、单位、时间归属和业务含义。字段字面相似不能替代语义核对。
- `property_type/type` 决定基础处理方式：字符串通常用于名称、编码、枚举和分组；数值用于有业务依据的聚合；时间字段用于范围、顺序和可比周期。
- `all_properties_required=true` 等价于所有 `properties` 均在 `required`；`all_properties_queryable=true` 等价于所有属性均可查询，它们是去重表达，不是字段裁剪。
- 数据源对象中的 `query_contract` 决定访问方式：`access_mode=uns_get` 使用 `relation_expression`，`access_mode=physical_table` 使用对应物理关系，时序来源使用独立时序协议。`datasource_identifier` 仅用于技术定位和血缘。当前详情的 `metadata_schema.properties` 是已加载字段定义，`all_field_names` 是该来源的字段发现索引。不得把相邻数据源字段串用，也不得把 `datasource_name` 当作表名或路径。
- `datasource_id`、`conversation_id`、`turn_id`、`execution_id` 和记录主键仅用于定位。不要从编号推导业务含义；用户可见内容优先使用 `datasource_name` 和 Schema 中的业务名称字段。

## Preparation 事实边界

- `analysis_semantic_context.business_questions` 共同构成本轮需要回答的业务范围；可以根据真实数据选择计算与表达方式，但不能静默只回答方便的一部分。
- `analytical_facets` 是开放式洞察方向，只帮助保持意图完整，不规定章节、指标或结论。
- `analysis_semantic_context.explicit_time_range` 非空时是用户明确要求的总体时间口径，正式查询必须保持；为空表示用户没有限定总体时间。为了趋势或对照扩展时间范围时使用独立查询，并清楚区分总体结果与背景比较，不能用扩展窗口替换用户总体。
- Schema 内的 `relationships` 用于定位来源关系。只有带已验证键证据的 `joinable` 关系可直接 JOIN；`unknown/alignable/not_joinable` 不得冒充已确认连接。
- 字段上的 `semantic_interpretation` 是 Preparation 模型解释，`origin=preparation_model`；`model_confidence` 仅是模型自评，不能确认业务定义、枚举含义或时间角色。以原字段定义、明确业务规则和实际观察判断其适用性；解释冲突时保留原定义，缺少依据时保留未知，不把推断当作事实。已有 `semantic_enrichment` 保留来源内容，但其中孤立的 confidence 也不证明语义已获认证。Schema 的 `relationships` 提供来源间已确认或待确认的关系。Runtime 的语义计划根据当前问题、Schema、总体和粒度确认计算规则。
- `semantic_enrichment_status=unavailable` 仅表示前置 Schema 补全未完成，不表示该数据源无数据或不可查询；继续依据原始 Schema 判断，必要时读取详情或 Probe。
- Schema 未说明的业务定义仍可能有歧义，不能用相邻字段或代理口径冒充。可由枚举、格式、时间覆盖或查询路径观察解决的技术未知可在 Runtime Probe 中确认；业务定义无法唯一确定时才澄清。
- `observed_value_profile`、`requested_value_resolutions`、`aggregation_safety` 和 `known_key_cardinalities` 是已记录的结构化事实。`empty/opaque_identifier` 不等于可直接用于正式筛选或展示；`requested_value_resolutions` 的 `exact/similar` 只有在其结构化结论明确确认业务等价时才可用于对应筛选，`unresolved` 仍需 Probe 或澄清。
- `aggregation_safety=unsafe_repeated_by_join` 表示该侧度量会在一对多展开后重复；先在原事实粒度聚合或去重。主体覆盖率的分子是主体键与辅助键交集的去重数，分母是主体人口键去重数。

## 数据源使用与披露

- 每次 `probe_analysis_data` 或 `submit_analysis_program` 的 `used_datasource_ids` 只填写代码真实读取或查询的当前范围内数据源；Probe 和 analysis 都遵守。
- 不为形式访问无关数据源，也不静默删除支撑用户明确业务问题的来源。决定不使用某来源时，应有字段、关系、口径、空集或不可用事实依据。
- 最终报告、图表和表格声称覆盖、查询或分析的数据源必须与真实访问范围一致。未查询来源不能写成“无数据”或“已分析”。
- 数据源路径不可访问、表不存在或查询错误属于 `data_source_unavailable`；查询成功且目标业务范围为空才属于无数据。
