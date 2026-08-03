# Unified Analysis Runtime Regression Audit

## Method

- Audit conversations one at a time across Preparation, Runtime execution, artifacts, report quality, and post-analysis Evidence.
- Record every sample before designing the next repair round.
- Treat a successful terminal status as insufficient when correctness, expressiveness, evidence completeness, or runtime performance is below the acceptance standard.
- Do not introduce conversation-specific rules. Final remediation must address shared contract or capability gaps.

## Conversation 20 / Turn 40

Question: `上半年签约套数有多少，年度达成率多少`

Run mode: replayed the captured Preparation result to isolate formal Runtime behavior.

Observed:

- Runtime datasource view correctly narrowed from `[35, 41, 78, 100, 116]` to `[35, 41, 78, 100]`; formal code actually used `[78, 35]`.
- The incompatible precomputed rate datasource `116` was no longer available to Runtime.
- Final values were internally coherent: first-half signed sets `481`, annual target `1,130`, derived completion rate `42.6%`, remaining target `649`.
- First formal execution failed `chart_semantic_error` because actual and target were placed as metric rows sharing one generic value column. The second execution used independent metric columns and succeeded.
- Total elapsed time was `76.8s`; Python executions themselves were `4.14s` and `3.14s`, so most time remained in model generation/repair and Evidence assembly.
- Final report was only `226` characters. It answered the numbers correctly but did not quantify the required second-half pace, compare first-half progress with elapsed time, or turn the target gap into a stronger management judgment.
- Evidence compilation was `partial`: `definitions=6`, `chart_refs=2`, `table_refs=0`, `report_refs=7`.

Assessment:

- Accuracy improved and the unsupported-source leakage is fixed for this contract shape.
- One-pass stability, report expressiveness, and Evidence completeness remain below acceptance.

## Conversation 21 / Turn 42

Question: `在途合同数和在途合同金额是多少`

Run mode: full Preparation and formal Runtime.

Observed:

- Total elapsed time was `260.5s`.
- Intent passed once. Datasource Selection needed a second attempt because the first output added unsupported `business_fact` properties to fact coverage items.
- Business Context required several calls. The first value-scope output used empty `included_values`; the first post-Probe output used unsupported `lineage_status=resolved` and placed the same status in both included and excluded sets.
- Final Runtime received a resolved categorical scope and answered instead of returning an unresolved explanation.
- The final scope counted `正常执行中` and `待变更/终止` as in-transit, producing `1,035` contracts and `51,504.12` ten-thousand yuan. It omitted `待执行`. This mapping is not sufficiently trustworthy: a pending termination/change state was included while a pending execution state was excluded.
- First formal execution failed the table metric-boundary validator because contract count and amount were represented as metric rows sharing a generic value column. The second execution succeeded with independent numeric columns.
- The report was concise (`249` characters) and answered the requested values, but most of its content explained the chosen scope rather than analyzing the population.
- Evidence compilation was `partial`: `definitions=2`, `chart_refs=0`, `table_refs=0`, `report_refs=2`. Both visible table metrics lacked table Evidence refs.

Assessment:

- A structured business-value scope is the right missing semantic capability, but the current first implementation is not stable or accurate enough to keep unchanged.
- The contract shape and generation guidance caused multiple Preparation retries, and the final semantic mapping remained questionable.
- Table metric-boundary guidance is still not being applied on the first Runtime attempt.
- Evidence assembly does not yet cover the structured table produced by the corrected Runtime code.

## Conversation 23 / Turn 44

Question: `年费合同情况如何`

Run mode: full Preparation and formal Runtime, terminated at the `360s` per-case limit.

Observed:

- The run never reached formal Runtime; no new execution row was created.
- Intent took `21.33s` and produced `2,749` characters. Selection took `49.32s` and produced `6,205` characters.
- Business Context responses were approximately `10,000` characters each and repeatedly failed contract validation.
- Failures included an unsupported `relationship_type=unknown`, empty fact bindings, a value scope referencing a field outside its binding, and derived bindings incorrectly carrying physical fields without a datasource.
- Business Context consumed several model calls around `45-50s` each and still did not converge before the limit.

Assessment:

- Embedding `value_scopes` into every fact binding is the wrong contract granularity. It expanded output and reasoning burden for an analysis where only a small subset of categorical concepts might need semantic resolution.
- This implementation made Preparation slower and less stable before it had a chance to improve Runtime accuracy.
- The current value-scope implementation should be removed and redesigned as a sparse, explicitly requested semantic-resolution result rather than another repeated property on every binding.

## Conversation 24 / Turn 45

Question: `发货情况如何，有什么建议吗`

Run mode: full Preparation and formal Runtime.

Observed:

- Total elapsed time was `286.7s`.
- Intent took `18.13s`; Selection took `44.33s`.
- Both Business Context attempts failed. The first response was `10,587` characters and produced nine validation errors, including an incomplete relationship, empty bindings, and an empty value scope. The second still emitted an incomplete relationship.
- Preparation correctly remained advisory and fell back instead of terminating the analysis, but Runtime consequently received less useful finalized lineage.
- First Runtime output ended without `save_analysis_result(...)`.
- Second Runtime execution declared `aggregation.grain=month` for a status-distribution query grouped by delivery status; the later monthly query was not independently registered. The validator correctly rejected this query/constraint mismatch.
- Third Runtime execution succeeded. It reported `347` contracts, `528` total sets, `206` shipped sets, and a `39.0%` shipped-set rate.
- The final report contained a core judgment and evidence-based suggestions, but three conclusion sentences were concatenated on one Markdown line, chart description was empty, and the narrative was materially thinner than the earlier accepted report for the same question.
- Evidence compilation was complete: `definitions=14`, `chart_refs=2`, `table_refs=4`, `report_refs=10`.

Assessment:

- Advisory fallback preserves availability, but it is masking a severe Business Context generation problem introduced by the expanded binding contract.
- Runtime still lacks reliable first-attempt discipline for assigning one independent query constraint contract to each physical query.
- Evidence can be complete when artifacts use stable column-level metric boundaries, so the post-analysis architecture is viable; completeness failures in other samples must be traced to artifact/query lineage shapes rather than treated as universal failure.
- Report formatting and narrative depth remain below the acceptance baseline even when the computation is correct.

## Conversation 22 / Turn 43

Question: `6月份销售情况如何`

Run mode: full Preparation and formal Runtime, terminated after `420s`.

Observed:

- Intent took `38.49s` and produced `3,444` characters.
- Datasource shortlist took `64.09s`; Selection took `58.32s` and produced `7,211` characters.
- First Business Context response took `55.41s`, produced `12,675` characters, and failed with `19` contract validation errors.
- Second Business Context response took `76.73s`. Its only remaining error was `probe_requests.top_n=100` exceeding the local maximum of `50`; the existing normalization validator accepts any positive integer and therefore did not clamp this recoverable value.
- Preparation then used advisory fallback rather than terminating.
- First Runtime code was `5,055` characters but omitted both `validate_query_constraints(...)` and `save_analysis_result(...)`.
- Second Runtime code expanded to `13,194` characters and failed because the declared start/end boundaries were not present in its validated SQL/parameters.
- The run did not produce a final analysis result before termination.

Assessment:

- Complex intent breadth is expanding Selection and Business Context outputs beyond a stable one-pass shape.
- A locally recoverable representation difference (`top_n=100`) unnecessarily discarded an otherwise improved Business Context result; bounded numeric fields need semantic normalization rather than a full LLM retry or fallback.
- Runtime generation becomes long and loses mandatory execution structure, indicating that API/protocol attention and multi-query planning are still not organized into a reliable code skeleton.
- This is a cross-stage failure, not a datasource-specific query bug.

## Conversation 25 / Turn 46

Question: `2026年营收和产品营收情况如何`

Run mode: full Preparation and formal Runtime.

Observed:

- Total elapsed time was `318.8s`.
- Shortlist correctly selected five candidates `[192, 188, 183, 184, 10]` and explicitly identified datasource `188` as the necessary product-classification source to complement revenue datasource `192`.
- Selection nevertheless narrowed the final scope to datasource `192` only. It marked product-line revenue facts as direct while its own reason was conditional: they were answerable only if `revenue_type` represented the business product-line taxonomy.
- Business Context Probe then established that `revenue_type` values were opaque integer hashes. Because Business Context cannot add a source omitted by Selection, product-line analysis became unresolved despite the correct supporting source having been present in the shortlist.
- Business Context itself required multiple contract attempts before resolution.
- Runtime executed three times: first failed end-boundary validation; second exposed an opaque identifier as a user-facing chart category; third removed the product structure artifact and succeeded.
- Final report correctly compared 2026 January-July revenue (`25,745,843.22`) with the 2025 comparable period (`59,635,781.48`) and reported a `56.8%` decline.
- The requested product-revenue analysis was replaced by a technical limitation section naming `revenue_type` and its hash encoding. This is not an acceptable completion of the original business question and exposes implementation vocabulary to the user.
- Evidence compilation was complete for the reduced result: `definitions=3`, `chart_refs=1`, `table_refs=0`, `report_refs=2`.

Assessment:

- The decisive failure is the handoff from shortlist to Selection. High-recall discovery found the needed semantic source, but final Selection discarded it based on an unverified proxy field in the primary source.
- Selection must not mark a fact `direct` when its support reason is conditional on unresolved field semantics.
- Later Probe and Runtime cannot recover from a missing datasource under the current architecture, so selection completeness must be solved before formal analysis.
- Evidence completeness on a reduced answer does not mean analysis completeness; acceptance must evaluate whether all user business questions were actually answered.

## Serial Recheck: Conversation 23 / Turn 44

Question: `年费合同情况如何`

Run mode: full Preparation and formal Runtime after replacing repeated binding-level value scopes with sparse Business Context semantic resolutions.

Observed:

- The run completed in `445.3s`, but does not pass acceptance.
- Intent took `22.81s`; Selection took `57.43s`.
- Business Context still made five LLM calls across structural retry, post-Probe finalization, and an outer semantic retry. Outputs were `6,071-6,866` characters. The nested retry control flow, not only model latency, amplified stage duration.
- The initial Business Context attempted to emit empty semantic resolutions before Probe. After Probe it also modeled the `subscription_cycle` string format as a business semantic population, although field-format information belongs in enriched Schema rather than a business filter contract.
- Runtime first generated PostgreSQL `::int` syntax for a federated MySQL source. The next repair changed it to another invalid `::signed` form, so the error feedback did not communicate the source SQL dialect precisely enough for one effective repair.
- A later Runtime attempt used a structurally invalid table with heterogeneous metrics sharing one generic numeric column; the metric-boundary validator correctly rejected it.
- The final attempt executed, but returned all-zero artifacts and a factually invalid report, including statements such as a Top1 customer having zero contracts and an asserted customer-concentration risk with no supporting rows.
- The zero result was accepted because an aggregate query returns one row even when `COUNT(*)=0`; checking only `DataFrame.empty` does not enter the existing no-data/Probe flow.

Assessment:

- Sparse semantic resolution is the correct granularity, but it must be available only in post-Probe finalization and only for business population filters, not formats or parsing instructions.
- Business Context needs a two-phase control flow: one lineage draft, then one optional Probe-backed finalization. A failed final semantic check may repair the final object, but must not regenerate the full draft and rerun Probe.
- Runtime repair diagnostics need to carry detected SQL dialect and the exact incompatible construct.
- Aggregate-zero handling must be part of the no-data/Probe protocol; otherwise technically successful empty aggregates can generate confident but meaningless narratives.
- Report quality cannot be accepted from section presence alone. Conditional findings must suppress claims whose supporting population is zero.

### Second serial recheck

- The rerun completed in `430.2s`; the final data was now coherent (`143` contracts, `8` customers) and post-analysis Evidence was complete (`definitions=5`, `chart_refs=2`, `report_refs=3`). It still fails performance and first-pass stability acceptance.
- Preparation consumed about `271s`: Intent `37.22s`, Selection `72.45s`, Business Context draft `55.88s`, post-Probe finalization `67.83s`, and finalization repair `35.98s`.
- The post-Probe finalizer twice invented incompatible semantic-resolution schemas. This experiment was removed: Probe enum/format results remain enriched field Schema, while Runtime retains final business-filter judgment.
- Runtime still required five formal/probe tool calls. Failures were a result-column mismatch, `idxmax()` on an empty grouped result, a no-data Probe transition, and a heterogeneous metric-row table rejected by the metric-boundary validator.
- The corrected final report had usable values and sections, but some interpretation and recommendations remained generic. Correct data plus complete Evidence is necessary but not sufficient for report-quality acceptance.
- A structural no-data gap was confirmed: an aggregate query with no matching source rows returns a non-empty one-row DataFrame containing zero counts. The Runtime previously treated that as success. Formal SQL loading now detects one-row unconditional count aggregates whose population counts are all zero and raises the standard `NoDataFoundError`; Probe and Preparation queries are excluded from this automatic routing.
# No-data 协议复核

## 原版职责边界

- `save_empty_analysis_result(...)` 不是正式分析主链路，只是兼容 helper。
- 正式代码在数据源成功访问、但查询/过滤/关联/聚合后没有可分析数据时，通过 `raise_no_data_error(...)` 返回结构化 `no_data_found`。
- Agent 根据反馈执行轻量 Probe 或口径纠偏；确认仍无数据并达到重试上限后，仍由统一洞察 Agent 调用 `save_analysis_result(...)`，生成包含查询范围、无数据原因和下一步建议的正式结果。多数据源场景可以用表格说明各数据源的实际查询状态，但不能生成全零伪图表。

## 当前发现的继承缺口

SQL 聚合查询在没有明细行时仍可能返回一行，例如 `COUNT(*) = 0`。原有空 DataFrame 检查无法识别这种“有效返回一行、实际业务人群为零”的结果，模型因此可能继续生成全零指标、排名或建议，绕过 `no_data_found`。

## 修复边界

- SQL Loader 解析非分组聚合中的无条件总体 `COUNT`，把“总体计数全部为 0”作为结构化执行诊断写入 `QuerySnapshot`。
- 不在单次 Loader 查询返回 0 时立即中断，避免破坏多数据源中“部分无数据、其余继续分析”的能力。
- `save_analysis_result(...)` 前综合本轮所有实际执行成功的 QuerySnapshot；只有全部实际查询均为空或总体计数为 0 时，才抛出 `NoDataFoundError`，重新进入原版 Probe/no-data 链路。
- 只要至少一个实际查询存在可分析数据，仍允许生成部分数据分析结果，并在报告中说明无数据部分。
- 不通过扫描报告文本中的数字判断无数据，也不新增独立的 no-data 结果类型。

## 串行复核：会话 23 / 轮次 44

- 总耗时：442.4 秒，未达到性能验收标准。
- Intent：17.1 秒；Selection：69.3 秒；Business Context 首次输出结构校验失败，随后重试并执行 post-Probe finalization，Preparation 成为主要耗时来源。
- Runtime 共执行 4 次：
  - 首次 `query_constraint_violation`：声明了 2024-01-01 至 2026-12-31，但 SQL 未落实边界。
  - 第二次按该范围查询返回空数据，正确进入 `no_data_found`。
  - 第三次 Probe 查明数据实际覆盖 2026-02-11 至 2033-06-21，并返回状态分布。
  - 第四次成功，但把正式范围改成 2026-02-01 至 2027-12-31，得到 96 个合同。
- 与此前可验证的全量口径 143 条不一致。用户问题“年费合同情况如何”没有明确时间范围，正式分析不应把到期日的任意窗口同时当成总体规模口径。
- 报告具备核心结论、结构、风险和建议，Evidence 编译为 partial（definitions=15、chart_refs=2、table_refs=0、report_refs=16），仍未达到完整证据链验收标准。

### 本轮设计结论

当前 `query_constraints` 只能证明“模型声明的范围已落实到实现”，不能证明“该范围来自用户明确约束，或只是用于趋势观察的分析窗口”。Probe diagnostics 也缺少范围用途边界，导致模型把数据覆盖范围写回正式总体口径。后续需要统一设计用户范围、总体基数范围和分析观察窗口的语义来源及承接方式，不能通过禁止所有自选时间窗口解决，因为时序趋势和开放式近况分析仍需要 Agent 合理选择观察窗口。

### Intent 分层与稳定性复验

- 原 Intent 只有 `required_business_facts`，开放式问题的报告丰富度只能被表达成必答事实。模型因此把“年费合同情况如何”扩成金额占比、同比、新签和续约等 4 组必答问题，Selection 被迫引入弱相关供应链来源，Runtime 又把趋势窗口错误施加到总体人口径，最终只统计到 4 条。
- Intent 现分为两层：`required_business_facts` 只承载用户问题不可缺少的最小选源事实；`analytical_facets` 承载正式分析可按真实 Schema 展开的规模、结构、趋势、异常和业务含义。Selection 只逐项覆盖前者，不能为 facet 引入代理来源。
- 查询约束同步区分基线与趋势：无显式用户时间范围时，`overall/detail/snapshot` 总体或明细基线不得使用推断窗口；日/月/年趋势查询仍可使用独立观察窗口。
- 第一次复验恢复正确总体 `143`，报告覆盖到期结构、风险判断和建议，Evidence 为 `complete`（definitions=10、chart_refs=3、table_refs=1、report_refs=7）。
- 第二次复验再次得到正确总体 `143`、未到期 `142`、已到期 `1`，并生成 2 个图表、2 个表格；Evidence 为 `complete`（definitions=9、chart_refs=2、table_refs=2、report_refs=7）。
- Probe 诊断中的 numpy/pandas 标量此前会触发 JSON 序列化错误，现由 `request_retry(...)` 的 API 边界统一转换为原生 JSON 标量，生成代码无需自行编写转换函数。
- `analysis_semantic_context.explicit_time_range` 现作为唯一用户时间口径传递。Intent 的自然语言定义和 Probe 覆盖范围都不能自行创造总体年度窗口；趋势观察必须保持独立查询。

### 粒度复验与 Intent 语义收敛

- 后续随机复验暴露出无源 Intent 仍会概率性把同一开放问题扩写成金额、续约或当前年度等硬事实。该职责现被结构性收回：Intent 仍输出业务目标、明确问题和 advisory `analytical_facets`，但每个问题的 Selection 义务被 canonicalize 为原业务问题本身。无源阶段不能再决定金额、状态、趋势等具体维度必须存在；Selection 根据完整 Schema 选择直接业务来源，正式 Runtime 保留洞察语义所有权。
- Selection/Business Context 对只读事实说明的回显采用 canonical ID 合并或丢弃，完全相同的事实绑定按事实 ID、数据源和字段集合合并；真正跨源冲突仍保留严格校验。
- 一对多关系编译器原来只覆盖 SQL 内直接聚合，未覆盖 JOIN 返回展开行后在 pandas 中 `len/value_counts/groupby.size` 的路径。现对 `overall/snapshot` 人口径补齐 pandas fanout 编译校验：主体统计必须先按主体键形成去重人口表；辅助明细数保持独立口径。
- `aggregation_safety.key_domain_coverage` 明确主体覆盖率分子必须是主体键与辅助键集合的交集去重数，不能把辅助表孤立键或一对多明细行计入主体覆盖。
- 最终复验得到合同实例 `143`、未到期 `142`、已到期 `1`、客户 `8`、合同编号 `55`、授权记录 `198`；报告包含状态、订阅周期、到期压力、客户集中度及事实支持建议。Evidence 编译为 `complete`（definitions=17、chart_refs=4、report_refs=19）。
- 该轮仍发生一次无数据后的混合 Probe/analysis 尝试。`no_data_found` 反馈现增加结构化 `recommended_next_execution_intent=probe`，明确下一版生成独立 Probe，禁止同一代码先探测再正式分析。
