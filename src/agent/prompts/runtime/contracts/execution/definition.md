# execution.definition

## 正式分析的实际执行定义

通过 `submit_analysis_program.definition_source` 一次提交完整 Python 定义模块。模块必须定义 `build_analysis(program, parameters)`；该函数登记真实查询、算法、判断和产物函数。宿主读取本次参数，调用该建图函数，执行查询与计算依赖，调用产物函数并保存为未定稿候选。随后同一 Runtime 会把本次事实交给你；在 `express` 状态撰写最终报告/文本/JSON并调用 `compose_analysis_presentation`，宿主只在此时将执行标为成功。不要另写主程序、手动执行/绑定流程或 `save_analysis_result`。完整 Schema、全部业务口径和当前结果协议继续适用。

- 模块可定义 `analysis_parameters_schema={"parameters":[...],"defaults":{...}}`。宿主取得本次参数后传入 build_analysis 和查询/展示函数。PythonCalculation 显式传 parameters=parameters；相对时间基于实际触发时刻。
- 定义 `load(parameters) -> DataFrame` 查询函数，内部使用当前物理查询 helper 与实际 query_constraints；通过 `program.register_query(source_id, load, grain=(...))` 登记，宿主随后调用。完整 SQL、参数化查询、分页、关联及任意 Python 保留；不要在查询函数里再写正式指标算法。未经修改的真实 helper 返回对象可保留原始查询回执，转换后的结果仅声明来源。
- 用 `program.register_calculation(DistinctCount(...), question_id="...")` 或 RowProductSum 登记原生算法；source_id 直接引用源或其他计算定义。自定义函数 `calculate(inputs, parameters)` 返回标量或 DataFrame，由 `program.register_calculation(PythonCalculation(id, calculate, input_source_ids=(...), parameters=parameters, value_field=..., unit=..., output_grain=(...), value_kind=...), question_id="...")` 登记。`inputs` 是命名的 CalculationInputs，唯一推荐读取方式为 `inputs.frame(source_id)`；它返回 `input_source_ids` 中该身份对应的 DataFrame，参数为字符串，不接受位置索引。为历史定义兼容，`inputs[source_id]` 的字符串键仍可用；`inputs[0]` 会立即给出使用 `inputs.frame(source_id)` 的明确错误。`parameters` 是本轮参数字典。输入身份和输出粒度由宿主冻结为不可变元组。`value_kind` 描述主结果字段的语义类型，不描述返回值形状：DataFrame 含分类/粒度列、主数值列及辅助列时，指定数值 `value_field` 并用 `value_kind="numeric"`，这样可表格化并参与数值比较；`output_grain` 声明其行粒度。只有结果没有主数值字段、确为日期范围或复杂对象表时，才设 `value_field=None, value_kind="object"`；此类结果保留供展示，但不能请求数值格式化或极值判断。算法只实现一次，复杂统计、预测和异常检测仍可使用完整 Python，不能缩减原分析范围。
- `program.register_transform(DecimalColumns(...))`、`program.register_transform(OneToOneJoin(...))` 登记真实转换；复杂变换可由 PythonCalculation 返回完整 DataFrame。宿主解析依赖，不要求声明顺序等于执行顺序，共享中间结果只计算一次。
- `program.register_extrema(decision_id, fact_id, mode="max")` 登记数学极值，保留全部并列对象。其他业务判断由有原始依据的算法求值；缺少判据时表达事实和限制，描述性统计不自动认证业务评价。
- 定义 `present(results, parameters)`，用 `program.register_presentation(present)` 登记唯一产物函数，返回当前 result.* 协议要求的 charts/tables。不要在此生成报告、自然语言结论或 text/JSON 正文；它们会由 `express` 阶段在模型看到本次事实后撰写。results 是已执行结果的读取接口：`table(id)` 是读取指标值和计算明细的唯一通用入口，返回计算结果 DataFrame 副本；标量计算通常在其中以 `value` 或声明的 `value_field` 列返回。`status(id)` 仅返回状态元数据 `{state, missing_count, input_rows, unit}`，不包含指标值，也不是 DataFrame；不要从该结果提取数值。`chart(id, helper="result", **原图表参数)` 直接构造图表，`decision_state(id)` 和 `matches(id)` 读取数学判断，`source_table(id)`、`source_observations(id)` 读取真实源及连接观测。对计算结果，优先使用登记时的 `result_id`；若不同问题有同名 `result_id`，使用 `(question_id, result_id)`。`PythonCalculation` 的 `definition_id` 是算法/依赖身份，只有在没有相应计划结果绑定时才直接用它读取。只在 `build_analysis` 中登记查询，宿主执行后再生成产物；不要重复发起物理查询或重做上游算法。
- API 身份和回调形状示例（字段名为占位符）：

  ```python
  def calculate(inputs, parameters):
      events = inputs.frame("events_source")  # 按声明身份取得 DataFrame
      return events.groupby("category").size().reset_index(name="count")

  program.register_calculation(
      PythonCalculation(
          definition_id="category_count_calculation",
          function=calculate,
          input_source_ids=("events_source",),
          parameters=parameters,
          value_field="count",
          output_grain=("category",),
          value_kind="numeric",
      ),
      question_id="question_1",
      result_id="category_count_result",
  )

  def present(results, parameters):
      counts = results.table("category_count_result")  # DataFrame，列含 category/count
      status = results.status("category_count_result")  # 仅状态元数据
      ...  # 由 counts 构造本轮所需 charts/tables
  ```

  算法依赖使用 `definition_id`；`register_extrema()` 与 `results.table()` 可使用内部 `definition_id` 或计划 `result_id`。若未限定的问题间存在同名计划结果，传 `(question_id, result_id)` 消除身份歧义。`inputs.frame(source_id)` 按声明身份取值；历史字符串下标保留兼容，位置下标会报明确接口错误。`status` 与数值表互不替代。
- 图表、表格和完整自由叙述继续使用当前 helper 与任意 Python。输出必须回答全部业务需求，所有动态判断由真实结果及实际有依据的条件决定。消费记录只证明值的来源，不认证周围解释；不能删减正文或指标换取覆盖。
- 外层 used_datasource_ids 声明真实访问范围。程序只读，不访问网络、进程或敏感资源，不硬编码凭据，不修改数据源。执行成功及局部血缘不等于业务验收通过。

如果用户所需的结论依赖高基数分布，必须在本执行定义中基于完整总体计算问题所需的汇总事实（例如完整分组计数/金额、可比期间变化或完整总体极值），并让这些结果作为已执行事实交给表达阶段。不要期待表达阶段从截断样本恢复总体统计；未定义或未执行的统计不能被报告为已知。汇总仅服务于本轮问题，不为所有数据添加无关指标。

## 执行结果驱动的报告发现

查询 `grain` 是输入记录唯一键，不能用后续分组键代替；未知时保留 `grain=()`。`output_grain` 才是结果粒度。
在 present 中用 `results.finding(finding_id, fact_id, kind=..., field=None, category_field=None, descending=True)`
绑定本问题需要的已执行发现：value 展示单行；ranking 完整排序并保留并列；range 全部极值对象及差值；scope 直接来源行数/粒度；table 完整结果表。
fact_id 支持计划身份，field 默认主数值列，ranking/range 需 category_field。发现自动进入 trace，表达可按身份引用。
API 不接收另写数值/行，不认证算法或业务解释；保留完整 Python、图表、表格与自然叙述。

## 物理读取约束

每条真实读取通过当前物理 helper 传入 query_constraints：time_range 为 None 或 {start,end,end_exclusive}；target_filters 为 {semantic_role,field_hint,value,match_mode} 对象列表；aggregation 为 None 或 {grain,metric}。实际 SQL、文件过滤、粒度和范围须与实际参数一致。目标总体成功查询但为空时使用 raise_no_data_error；技术失败不伪装为零值或无数据。部分路径为空保留成功路径并披露影响。

<!-- runtime-core-sections -->

## 函数参考

执行证据由 Runtime 同步记录，展示直接消费保存的计算结果。`results.view(result_id, columns=[...], rename={原字段:显示字段})` 只选列/改名并保留消费身份；现有表格和图表 helper 可直接读取该视图。保存结果视图的 copy()/sort_values() 保留宿主记录的消费身份；改写单元格或任意未登记变换不会继承它。新的汇总、比率或统计仍登记为 calculation；依赖和展示共享结果身份解析，可用实际计算 ID 或唯一的计划 result_id，不在展示函数中重新定义。`results.finding(...)` 保存比较/范围等展示操作，express 可引用它。查询、计算、展示回执用于追溯实际执行，不表示业务语义已验证。

<!-- runtime-helper-reference -->

直接使用下列已注入原生定义，不需要构造另一层 JSON 程序或再生成等价公式。完整查询方言及产物参数由 query.*、result.* 提供；当前端口仅交付实际定义，历史源码接口由 execution.implementation 承接。

## 模块交付示例

以下只演示原生模块接口；源路径、字段和问题 ID 必须来自当前 Schema 与需求，完整实现仍须覆盖全部指标、算法和叙述，不能照搬示例而缩减分析。

```python
def build_analysis(program, parameters):
    def load(parameters):
        return query_local_file(
            query_key="population",
            file_path="<当前 Schema 的文件路径>",
            query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
        )

    program.register_query("population", load, grain=("entity_id",))
    program.register_calculation(
        DistinctCount("counts", "population", "entity_id", group_by=("category",), unit="个"),
        question_id="<当前问题 ID>",
    )
    program.register_extrema("highest", "counts")

    def present(results, parameters):
        return {
            "charts": [results.chart("counts", chart_kind="bar", title="分类数量",
                                     category_field="category", value_field="value")],
            "tables": [build_table_result(title="分类数量", data=results.table("counts"),
                       columns=[{"key": "category", "label": "类别"}, {"key": "value", "label": "数量"}])],
        }

    program.register_presentation(present)
```
