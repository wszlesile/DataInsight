# context.time_series

## 查询模式

原始值保留查询范围内的实际观测；采样按窗口选取代表点，插值按声明的方法补点；统计聚合计算窗口内总体的均值、最大值、最小值等统计量。它们回答不同问题，窗口长度相同也不能互相替代。语义规划在本问题的事实需求和计算口径中保留用户选择的模式、窗口及方法；后续实现承接这一选择，额外统计作为另外的分析量声明。

每条正式时序查询在 `query_constraints.time_series_query` 先声明语义，再写对应方言：

```python
# 原始值
{"mode":"raw","decision_source":"analysis_choice"}
# 采样/插值
{"mode":"sample","window":"1m","interpolation":"previous","decision_source":"explicit_user"}
# 统计聚合
{"mode":"aggregate","window":"1m","aggregations":[{"field":"value","method":"avg"}],"decision_source":"explicit_user"}
```

- `decision_source` 仅为 `explicit_user/system_policy/analysis_choice`。
- 用户明确要求 raw、采样/插值或均值/最大/最小等统计时采用对应模式。短时间只要求历史趋势且未指定采样/统计时保持 raw。
- 范围超过一天且用户未指定模式时，使用原生采样或统计控制点数并披露窗口和方法；不能用 `LIMIT` 冒充降采样。
- 采样、插值和统计必须由时序库原生完成，不能先取全部原始点后用 pandas `resample/floor/groupby/mean/max/min` 替代。
- 必须有时间范围，开始/结束真实出现在内部 SQL。时间字面量使用 Asia/Shanghai 偏移，例如 `+08:00`，不用 `Z`。
- 返回时间可以带 UTC 或其他偏移。形成显示标签之前，按本轮展示时区转换带时区时间，例如 `pd.to_datetime(series, utc=True).dt.tz_convert("Asia/Shanghai")`；无偏移的时间先按来源声明时区解释。图表、明细表和报告共享保存的同一时间标签，不在正文中单独加小时偏移。原生窗口的锚点、偏移和两端部分窗口由实际查询决定，窗口条数与请求时长不能替代实际时间点/窗口边界的覆盖分析。
- 时序来源可能有多个值字段。按问题选择相关 `value_fields`，不能只默认第一个字段。
- 好值通常按 `quality_field == 0`；用户明确分析全部质量或坏值时才放宽。
