# execution.numeric_interpretation

## 数值关系与局部表达

这些函数可用于实现计划中的比较和局部发现，不定义报告章节、业务阈值或评价依据。图表、表格和报告仍复用同一计算结果。

`evaluate_numeric_comparison` 一次返回关系、互斥谓词、差额与相对变化。分支选择读取返回的 `less/equal/greater`，比较文字读取同一结果的 `relation`，缺失关系为 `None`，不是“不高于”。

`summarize_extrema` 一次返回完整极值行和 `kind`。它只接受数据与数值字段，名称列仍保留在返回的 `rows` 中；`single` 是仅有一个可比对象，`all_equal` 是全部可比对象相等，均不能解释为独占领先。

```python
extrema = summarize_extrema(grouped_result, "count", mode="max")
matched_names = extrema["rows"]["name"].tolist()
```

`render_extrema` 可直接按完整比较结果生成事实表达，默认覆盖所有数学情形，不必编写模板。需要自定义局部文字时，`render_numeric_comparison` 的四种表达必须完整提供；`render_extrema` 的五种表达可按需覆盖，其余情形保留默认事实表达。模板使用普通字符串及函数定义的命名占位符，只有被选中的情形会格式化。它们只返回一段发现，报告主线和业务解释由程序组织。

此处直接调用的底层 `render_extrema` 可传 `unit`；执行定义展示接口 `results.extrema(decision_id, label_field, value_label="", language="zh")` 是另一层 API，不接受 `unit`，而是继承数值定义中的单位。不要混用两个签名。

```python
finding = render_extrema(
    grouped_result, "count", "name", separator="、", unavailable="不可计算",
    empty="当前没有可比较的记录。",
    single="仅有 {labels} 一个可比较对象，数量为 {value:g}，无法作对象间排名。",
    unique="{labels} 数量最多，为 {value:g}。",
    tied="{labels} 并列最多，各为 {value:g}。",
    all_equal="{valid_count} 个对象的数量均为 {value:g}：{labels}。",
)
```

文本由分析程序提供，函数只负责按数学情形选择和填值，不认证文本中的业务判断。比较精度、总体和单位由实际算法决定；零基期的相对变化不可计算，与数值大小关系分别处理。

## 函数协议

<!-- runtime-helper-reference -->
