# execution.implementation

## 正式分析程序闭环

- 当前工作提交一份完整、独立、只读的 Python 洞察程序。它在同一代码单元完成参数、查询范围、真实读取、计算、空数据判断、当前输出协议要求的产物与一次保存。
- 不继承 Probe 或旧程序的变量、DataFrame、代码片段和推测；可复用的只有结构化 Probe 事实、Preparation 事实和当前精确 Schema。
- 先定义 `analysis_parameters_schema={"parameters":[...],"defaults":{...}}`，再用 `analysis_parameters=get_analysis_parameters(...)` 获取本次值。相对时间必须从运行时触发时刻派生，不能固化任务创建时间。
- 每条物理读取都有稳定 `query_key` 和结构化 `query_constraints`：`time_range` 为 `None` 或 `{start,end,end_exclusive}`；`target_filters` 是语义角色、字段、值、匹配方式对象列表；`aggregation` 是真实返回粒度/指标或 `None`。实际 SQL、文件过滤和计算必须与之对应。
- 查询后先确认真实列、类型、粒度和总体，再做计算。多维结果保留必要维度；一对多关系先在事实粒度聚合，避免重复计量。比较窗口保持可比，比例、百分点差和相对增幅必须区分。
- `success` 但目标总体确为空时调用 `raise_no_data_error(...)`；技术失败不能伪装成零值或无数据；部分路径为空时保留成功路径并披露影响。
- 图表、表格、叙事复用同一已求值计算变量、总体、筛选与粒度。业务名称用于标题/表头/报告；不要产生空章节、空产物、内部变量名或指标说明书式报告。
- 只调用一次 `save_analysis_result(...)` 收口。`used_datasource_ids` 必须等于真实访问范围。修复涉及查询、字段、JOIN、算法、报告或产物时提交完整代码；只对反馈明确的孤立 API 调用问题使用局部 patch。

<!-- runtime-core-sections -->

## 可直接调用的函数

### 查询口径对象

每条读取的 `query_constraints` 是下面结构的 Python 对象；字段和值来自本次真实查询，示例仅说明 API 数据类型。

```python
query_constraints = {
    "time_range": None,
    "target_filters": [
        {"semantic_role": "business_category", "field_hint": "category",
         "value": ["A", "B"], "match_mode": "exact"},
    ],
    "aggregation": {"grain": "category", "metric": "SUM(amount)"},
    "notes": "按所选业务类别汇总金额",
}
```

- `time_range`：无时间限制为 `None`，否则为 `{"start":"起点时间字符串","end":"终点时间字符串","end_exclusive":True}`。
- `target_filters`：无对象筛选为 `[]`；每项包含非空字符串 `semantic_role`、`value` 和 `match_mode`。`value` 为非空字符串或非空字符串列表；`match_mode` 为 `exact` 或 `fuzzy`；可选 `field_hint` 为真实字段名字符串。
- `aggregation`：无聚合为 `None`，否则为对象，`grain` 和 `metric` 为实际返回粒度和聚合指标的字符串说明。
- `notes`：可选字符串。数值范围、比较谓词等实际实现通过查询 helper 的 `filter_expressions` 记录；`target_filters` 的 `value` 不承载运算符对象。

<!-- runtime-helper-reference -->

当前查询类型和结果格式的精确方言、函数参数与边界细则由相应 `query.*`、`result.*` 权威参考定义。当前工作缺少某项实现细节时读取该参考，不以猜测替代。
