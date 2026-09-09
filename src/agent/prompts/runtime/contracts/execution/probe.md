# execution.probe

## Probe 工作闭环

- 当前工作只验证一个明确的技术未知：字段归属、真实枚举、存储格式、时间覆盖、查询路径或某个筛选为何为空。
- 每次 Probe 是独立 Python 执行单元；不得依赖前一次变量、DataFrame、import 或 stdout。
- 使用 `probe_analysis_data` 提交有界观察代码。代码只读取并打印/返回必要的结构化观察；不计算正式指标、不构造图表表格、不保存报告。
- 关系型 Probe 的读取 API 是 `probe_data_with_sql(...)`，不是未定义的 `query(...)`。它返回 `ProbeQueryOutcome`；先检查 `outcome.status`，成功时只从 `outcome.data` 取得 DataFrame。例如：`outcome = probe_data_with_sql("SELECT category, COUNT(*) AS record_count FROM schema_name.table_name GROUP BY category")`，随后仅在 `outcome.success` 时访问 `outcome.data` 并打印有限的统计或样例。
- 在同一批内逐项检查相互独立的候选路径。`success + row_count>0` 表示可用数据，`success/no_data + row_count=0` 表示成功空集，`query_error` 是技术失败，不能改写成无数据。
- Probe 成功后，后端把查询快照、列、行数、有限样本和字段剖面写成事实账本，并恢复发起探测前的工作。账本只证明已观察的范围，不能替代正式计算；如仍有新的技术未知，可显式选择相应的探测焦点。
- 如果全部必要路径都成功且业务范围为空，使用 `raise_no_data_error(...)`；表不存在、认证、路径或查询失败不属于无数据。

<!-- runtime-core-sections -->

## 可直接调用的函数

<!-- runtime-helper-reference -->

查询方言、源类型读取函数和复杂 Probe 示例由对应 `query.*` 权威参考定义；只有当前工作需要该细则时读取，不猜测字段、方言或业务枚举。
