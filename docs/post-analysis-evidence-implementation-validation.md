# 后置 Evidence 实现与验证记录

本文记录 `post-analysis-evidence-architecture.md` 的当前实现事实和回归边界。设计原则仍以架构文档为准；本文用于判断实现是否偏离设计。

## 1. 当前实现链路

正式分析成功后，系统冻结同一次 execution 的生成代码、查询快照、数据源快照和最终 artifacts。`PostAnalysisEvidenceCompiler` 只读取这些事实，不修改报告、图表、表格或正式分析状态。

本地编译分为以下步骤：

1. 数据访问 helper 按统一 QuerySnapshot 协议为每次真实读取记录独立物理执行事实，包括 loader、实现、执行状态、返回字段和规模。
2. 所有数据源先由 QuerySnapshot 的 loader 身份和 `execution.columns` 建立通用查询与字段血缘；关系 SQL 再使用 `sqlglot` 增强投影、聚合、筛选、分组和排序语义。DBAPI 参数占位符只在解析副本中规范化，不修改真实 SQL。
3. Python 使用标准 AST 建立查询、DataFrame、列别名、聚合、公式、排序和产物字段之间的血缘。
4. 只有一个产物位置能够证明唯一计算边界和完整查询来源时，才生成不可变 `CalculationCandidate`。
5. Evidence Planner 只补充业务名称、业务解释、值类型和展示单位，不能选择、合并、拆分或重建计算关系。
6. Compiler 生成 EvidenceDefinition 和 refs，Assembly 按真实 artifact ID 持久化。

## 2. 查询事实

`validate_query_constraints(...)` 记录的是查询范围声明；真实数据访问 helper 记录物理执行。存在多次物理读取时，每次读取都有独立 query key，并继承对应的已验证范围。该事实协议不以 SQL 为中心：关系表、VxBase 时序查询和本地文件使用同一组实现与执行字段。

当 execution 中存在成功的物理执行快照时，Lineage Indexer 只使用这些成功快照建立数据血缘，避免把未执行的多语句范围声明误当成数据来源。

`load_local_file(...)` 和 `load_local_file_low_memory(...)` 记录独立读取快照；VxBase 等时序 loader 也按相同协议记录实现和真实返回字段。低内存迭代器在消费结束后记录总行数与字段；中途异常则记录失败状态。DataFrame 只携带轻量 `data_insight_query_key`，不会把数据集复制进 Evidence。

## 3. 产物与计算边界

- 图表按数值字段绑定，不按数据点生成 Evidence。
- 表格按业务数值列绑定，不按单元格生成 Evidence。
- 报告通过代码中的动态表达式锚点定位最终精确文本区间，不扫描 Markdown 猜测数值语义。
- 相同计算签名可在报告、图表和表格之间复用同一个 Evidence。
- SQL 聚合、本地聚合和派生公式属于不同计算边界，即使业务名称相似也不能复用。
- 一个展示字段由多个独立 series value 构造时，标记为歧义计算边界，不生成 Evidence；不能把共同 query load 冒充唯一计算。
- artifact 绑定优先使用数据库 artifact ID。类型与序号只用于无 ID 的兼容定位，输入列表顺序不会改变引用归属。

## 4. 保守边界

这里的“保守”只作用于 Evidence 覆盖率，不限制正式分析表达：

- 无法证明唯一边界时，该位置不提供证据链点击。
- 不按字段名、业务词、数值相等或主题相似度猜测 Evidence。
- Evidence 编译失败只更新 sidecar 状态和诊断，不回滚成功分析，也不触发正式代码重试。
- 不通过 Prompt 禁止报告数值、复合指标、比较、排名或建议。

## 5. 真实执行验证

2026-08-01 使用成功 execution `2953` 在当前代码上重新编译并持久化 Evidence，输入 artifacts 特意采用反向顺序：

- 编译状态：`complete`
- EvidenceDefinition：6
- 报告 refs：6
- 图表 refs：1
- 混合单位“销售关键指标”图：0 refs，未误绑共同查询
- “各销售经理毛利贡献”图：1 ref，绑定实际经理聚合 SQL
- 合同总金额、合同数量、业绩总套数、毛利总额：分别绑定同一实际查询中的不同 SQL 投影边界
- Top 销售经理毛利额：报告与经理图复用同一 Evidence
- Top 销售经理毛利占比：使用经理聚合与总体聚合两个真实查询形成独立公式 Evidence
- 引用最终落到真实 report/chart artifact ID，未受输入顺序影响

这次验证只重编译 Evidence sidecar，没有重新运行或修改正式分析结果。

2026-08-02 使用固定 Preparation 对 execution `3042`、`3044` 和 `3047` 验证正式 Runtime 与后置 Evidence：

- `3042`：17 个 EvidenceDefinition、4 个图表 refs、4 个表格 refs、22 个报告 refs，状态 `complete`。
- `3044`：13 个 EvidenceDefinition、3 个图表 refs、4 个表格 refs、7 个报告 refs，状态 `complete`。
- `3047` 的初始报告包含动态精度和预格式化百分比；修正结构模板身份后，本地重编译得到 14 个报告 refs，百分比沿公式回溯，数字形态的分类编码未被当作指标。
- 报告月份、年份等参数不生成 Evidence；图表按指标列、表格按业务数值列绑定，不按点或单元格创建 Evidence。
- 通用字段血缘已覆盖关系查询实际列、本地文件 loader 和 VxBase 外层 `SELECT *` 返回列；SQL AST 只增强可解析关系查询。

## 6. 自动化保护

聚焦测试覆盖：

- SQL 聚合与本地公式边界拆分
- 相同计算跨产物复用
- 同名字段在不同查询中的来源消歧
- 图表和表格字段别名传播
- 本地文件与低内存读取快照
- VxBase/时序返回字段与外层 `SELECT *` 的通用字段血缘
- 动态精度 f-string、预格式化百分比和数字形态维度编码
- 混合计算展示列不误绑
- artifact ID 稳定定位
- Planner 无效引用或调用失败时不猜测 Evidence
- Evidence 编译失败不污染成功分析
