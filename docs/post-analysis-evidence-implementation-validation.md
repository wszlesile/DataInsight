# 后置 Evidence 实现与验证记录

本文记录 `post-analysis-evidence-architecture.md` 的当前实现事实和回归边界。设计原则仍以架构文档为准；本文用于判断实现是否偏离设计。

## 1. 当前实现链路

正式分析成功后，系统冻结同一次 execution 的生成代码、查询快照、数据源快照和最终 artifacts。`PostAnalysisEvidenceCompiler` 只读取这些事实，不修改报告、图表、表格或正式分析状态。

本地编译分为以下步骤：

1. 数据访问 helper 按统一 QuerySnapshot 协议为每次真实读取记录独立物理执行事实，包括 loader、实现、执行状态、返回字段和规模。
2. 所有数据源先由 QuerySnapshot 的 loader 身份和 `execution.columns` 建立通用查询与字段血缘；关系 SQL 再使用 `sqlglot` 增强投影、聚合、筛选、分组和排序语义。DBAPI 参数占位符只在解析副本中规范化，不修改真实 SQL。
3. Python 使用标准 AST 建立查询、DataFrame、列别名、聚合、公式、排序和产物字段之间的血缘。
4. 图表和表格只有在产物字段能够证明唯一计算边界和完整查询来源时，才生成不可变
   `CalculationCandidate`。
5. 报告 Evidence 采用“位置高召回、AST 主导、LLM 补漏”协议：AST 与 QuerySnapshot 先绑定
   所有本地已证明的报告数值；最终 Markdown 扫描器补充 AST 未定位的数值位置；Evidence
   Planner 读取完整成功 Python 代码、未决候选、源码表达式锚点、operation 目录和成功
   QuerySnapshot，补充 AST 无法覆盖的报告语义。
6. Compiler 生成 EvidenceDefinition 和 refs，Assembly 按真实 artifact ID 持久化。

### 1.1 报告发现与本地验真边界

当前实现已切换为“AST 主导绑定，LLM 语义补漏”：

- 本地 AST 对已经能证明 `report slot -> operation -> QuerySnapshot` 的报告数值直接生成 Evidence；
- 最终报告扫描器高召回枚举尚未被 AST 定位的数值位置，只生成候选 ID 和精确字符区间；
- 年月日、Markdown 列表序号、比较阈值、公式系数和业务编码由确定性展示语法排除，
  `共81批`、`温度2次` 等紧邻中文文本的业务数值必须进入候选；
- Planner 以带行号的完整成功代码为主要事实，只处理 AST 未覆盖或语义边界仍需判断的源码 slot；
- `source_anchor_catalog` 是动态字符串表达式的语法索引，不是报告指标候选；其中的
  `verification_hints` 仅描述本地已知可达操作，Planner 可以独立复核、拆分或不采用；
- 即使本地没有预先生成 report output，仍可调用 Planner；无法把发现结果验真到最终报告位置时，
  只将该 source anchor 标记为 unresolved，不伪造 ref，也不影响图表和表格 Evidence；
- 最终 query refs 不由 Planner 决定，而是由其所选 operation 到成功 QuerySnapshot 的依赖闭包生成；
- 最终 Markdown 用于发现和定位候选，不负责决定指标语义；不得通过数值相等或文本相似建立绑定。

单个报告指标发现或验真失败只降低补漏覆盖率。Planner 超时或结构错误时，本地已证明的报告、
图表和表格 Evidence 仍沿确定性路径生成，正式分析 execution 不会被回滚或进入代码修复。

## 2. 查询事实

`validate_query_constraints(...)` 记录的是查询范围声明；真实数据访问 helper 记录物理执行。存在多次物理读取时，每次读取都有独立 query key，并继承对应的已验证范围。该事实协议不以 SQL 为中心：关系表、VxBase 时序查询和本地文件使用同一组实现与执行字段。

当 execution 中存在成功的物理执行快照时，Lineage Indexer 只使用这些成功快照建立数据血缘，避免把未执行的多语句范围声明误当成数据来源。

`load_local_file(...)` 和 `load_local_file_low_memory(...)` 记录独立读取快照；VxBase 等时序 loader 也按相同协议记录实现和真实返回字段。低内存迭代器在消费结束后记录总行数与字段；中途异常则记录失败状态。DataFrame 只携带轻量 `data_insight_query_key`，不会把数据集复制进 Evidence。

## 3. 产物与计算边界

- 图表按数值字段绑定，不按数据点生成 Evidence。
- 表格按业务数值列绑定，不按单元格生成 Evidence。
- 报告通过代码动态表达式锚点和最终产物高召回扫描共同定位数值位置；扫描不猜测数值语义。
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

## 7. 2026-08-05 真实回归

使用最近成功 execution 的冻结代码、QuerySnapshot 与 artifacts 做只读复编译，未更新正式分析结果：

- `651`（多源质量根因分析）连续两轮均为 `complete`：43 个 EvidenceDefinition、3 个图表 refs、
  14 个表格 refs、33 个报告 refs，50/50 个结构化与报告指标位置全部覆盖，无悬空引用。
- `651` 中同一句内的“事件总次数”和“涉及批次数”稳定落到不同计算操作；循环中的过程报警、
  温度报警、物料批次等字段按实际源字段和计算参数形成独立 Evidence，不再因复用循环代码误共享。
- `629`、`591` 均为 `complete`，分别覆盖 25/25 个指标位置；`675` 为 `complete`，覆盖 45/45。
  `675` 中同一 `len(device_types)` 在报告不同位置复用同一 Evidence，属于相同计算的合法复用。
- `698` 初次暴露 Planner query 选择与操作血缘不一致。调整为由最终操作闭包 query refs 后，复编译
  为 `complete`：25 个 EvidenceDefinition、2 个图表 refs、103 个报告 refs，105/105 全覆盖。
- `671` 保持 `partial`：已覆盖 116 个报告 refs、7 个图表 refs、10 个表格 refs；8 个本地常量位置
  没有 QuerySnapshot 事实，明确列为 unresolved，没有冒充查询证据。

自动化回归覆盖参数化循环、故意合并不同报告指标、错误 query 选择、常量隔离、自定义 helper、
图表/表格结构化引用及 Preparation/Runtime 相关契约。真实回归同时检查 Evidence key 存在性、
query refs 有效性及不同已证明计算边界的误共享。

## 8. 2026-08-06 候选补漏协议验证

- 生产 Planner payload 已移除 `output_catalog` 和 `calculation_candidates`，只传递完整成功代码、
  源码表达式锚点、operation 事实、成功 QuerySnapshot 与必要的数据源字段语义。
- 增加“AST 未产出任何 report output”用例，确认 Planner 仍被调用；本地无法证明最终位置时状态为
  `partial` 且不生成虚假 ref。
- source anchor 的语义分类与 output node 的物化校验已经分离。年份、排名、标签等非指标 slot
  不会因旧 output 完整性校验被误判，无法到达查询的业务数值单独进入 unresolved。
- 参数化循环的绑定进入计算签名；同一源码 slot 在不同字段或参数绑定下不能误共享 Evidence。
- 聚焦 Evidence 单测 86 项通过；连同查询血缘 helper 和本地重跑边界的集成测试共 124 项通过。
- 冻结 execution `719` 只读复编译得到 15 个 EvidenceDefinition、4 个图表 refs、3 个表格 refs、
  14 个报告 refs；实际报告 slot 全覆盖，状态 `partial` 仅因一个既有表格列无法证明唯一计算边界。
- 冻结 execution `721` 只读复编译为 `complete`：20 个 EvidenceDefinition、4 个图表 refs、
  7 个表格 refs、19 个报告 refs。
- 参数化循环复杂样本 `651` 首轮暴露 Planner 遗漏和重复实例声明。实现改为首次覆盖遗漏触发一次
  Evidence 专用修复，修复后仍有遗漏或冲突则按 slot 隔离，不能清空合法报告 refs。最终只读复编译
  为 `complete`：43 个 EvidenceDefinition、3 个图表 refs、14 个表格 refs、33 个报告 refs，
  0 unresolved、0 missing。

## 9. 2026-08-06 AST 主导、LLM 补漏验证

真实回归显示，LLM 主导报告 Evidence 识别会把本地已经证明的报告数值暴露给 Planner 漏分类风险。
实现已调整为 AST 与 QuerySnapshot 先绑定可证明报告数值，Planner 只补充 AST 覆盖不到的语义缺口。

- `719`：`complete`，16 个 EvidenceDefinition、4 个图表 refs、4 个表格 refs、14 个报告 refs。
  本轮补齐了 `groupby(...).agg(["sum", "mean", "count"]).reset_index()` 后整体 `columns = [...]`
  重命名的多聚合位置血缘，表格 `平均功率_kW` 不再漏绑定。
- `591`：`complete`，21 个 EvidenceDefinition、4 个图表 refs、21 个报告 refs。
- `629`：`complete`，25 个 EvidenceDefinition、3 个图表 refs、22 个报告 refs。Planner 返回未知
  unresolved id 时不再让已证明 Evidence 降级为 planner unavailable。
- `721`：`complete`，20 个 EvidenceDefinition、4 个图表 refs、7 个表格 refs、19 个报告 refs。
- `651`：`complete`，45 个 EvidenceDefinition、3 个图表 refs、14 个表格 refs、33 个报告 refs。
- `671`：保持 `partial`。缺口集中在 `overall_alarm_pct` 同时进入图表和表格，但正式代码中该字段来自
  硬编码常量字典而不是成功 QuerySnapshot。后置 Evidence 不为硬编码分析数值伪造数据源证据，这是
  正确暴露正式分析代码质量问题，不属于 Evidence 编译器漏绑。
## 10. 2026-08-06 三级可信边界回归

实现已补充 `verified / attested / contradicted` 三态：

- LLM 只处理本地未覆盖的报告候选，返回结构化候选 ID、查询 ID、字段、计算说明和置信度。
- 本地可证明时继续使用 `verified`；本地语法能力缺口不再自动否定高置信、事实一致的 LLM 声明，而是生成 `attested`。
- 低置信声明、未知查询、未知字段、越界候选以及与本地已证明血缘冲突的声明均拒绝并保留明确诊断。
- 恢复型 Planner 只能认领本次输入目录中的候选，不能重新覆盖已经完成的本地绑定。
- 报告数值扫描补充了范围分隔符、公式系数和字母数字业务编码的确定性消歧。

当时自动化回归共 `151 passed`。冻结 execution 只读重复编译结果：

| execution | status | definitions | chart refs | table refs | report refs | 说明 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 731 | complete | 35 | 4 | 0 | 109 | 全部 verified |
| 732 | complete | 29 | 4 | 0 | 61 | 重复运行无 Planner fallback |
| 735 | complete | 35 | 8 | 10 | 81 | 33 verified + 2 attested（一次回归）；重复运行保持 0 unresolved |
| 736 | complete | 45 | 4 | 7 | 47 | 2026-08-07 写回结果；列表序号 0 引用，排序段落业务数值完整覆盖 |

真实回归中，Planner 单次耗时约 6.5-16.9 秒；此前约 30-98 秒的主要原因是恢复请求重复携带 operation catalog、候选目录排在完整代码之后，以及 QuerySnapshot 缺少返回字段语义。当前消息顺序为“候选与查询事实 -> 完整代码 -> 候选 ID 提醒”，并只保留恢复所需的查询字段信息。

当首次结构化声明在本地物化后因未知字段、源码定位或候选漏分类而产生未决项时，只允许一次 Evidence 专用语义修复。修复请求携带未决候选、拒绝原因和原查询字段目录，不重新执行正式分析；仅当未决数量严格下降时采用修复结果。查询字段在错误 query 中不存在、但在全部成功 QuerySnapshot 中仅有一个查询完整包含这些字段时，Compiler 可确定性归一到该唯一查询；零个或多个候选仍拒绝。

## 11. 2026-08-07 最终产物高召回验证

会话 46 的 execution `736` 暴露了两个互补的报告位置问题：

- `- **1. 物料批次**` 中的动态 `rank` 来自 DataFrame 列而不是 `enumerate(...)`，旧 AST
  规则将其误当成指标。当前实现按动态槽位的 Markdown 行首位置和有序列表分隔符识别展示
  序号，不依赖 `rank` 字段名，因此不会排除表格中的真实排名指标。
- 旧高召回扫描器使用 Python `\w` 保护标识符边界，而 `\w` 会包含中文，导致 `共81批`、
  `总计10次`、`温度2` 等真实业务数值没有进入候选。当前边界只阻止 ASCII 标识符和小数
  内部切分，中文相邻数值正常进入候选；业务编码继续由独立的确定性规则排除。

重新编译并写回后的结果为：`complete`、45 个 EvidenceDefinition、4 个图表 refs、7 个
表格 refs、47 个报告 refs、0 unresolved。根因候选排序段落中的 6 个展示序号均无 ref，
其余 16 个业务数值位置全部有 ref；分值、物料批次数、复检次数和各参数报警次数按真实字段
形成各自计算边界。自动化回归共 `153 passed`。
