# query.time_series

## 查询 API

<!-- runtime-helper-reference -->

本协议覆盖 `timescale` 与 `vxbase`。两者都通过 `load_data_with_sql(...)` 执行，外层使用：

```sql
SELECT * FROM ts_query('<ts_query_source>', '<escaped_inner_sql>')
```

内部表名来自当前来源 `query_contract.datasource_identifier`，字段来自同一来源 `query_contract.allowed_fields/value_fields`。

<!-- time-series-mode-semantics -->

## Timescale

- 物理时间字段为 `"timeStamp"`。标识包含大写、`$` 或其他特殊字符时，内部 `FROM` 表名必须双引号原样引用。
- 质量、点位和值域过滤优先写在内部 SQL。`status = 0` 不要放到 `ts_query('timescale', ...)` 外层，除非内部已经投影该字段且确有外层处理需要。
- 原始趋势常用内部 SQL：

```sql
SELECT "timeStamp", value, status
FROM "<datasource_identifier>"
WHERE "timeStamp" >= '<start+08:00>'
  AND "timeStamp" < '<end+08:00>'
  AND status = 0
ORDER BY "timeStamp" ASC
```

- 采样在内部使用 `time_bucket(...)`、`time_bucket_gapfill(...)` 或 `date_bin(...)` 形成窗口，并使用 `first/last/locf/interpolate` 等点值选择或补点能力。
- 统计查询同时使用时间窗口、用户指定的 `AVG/MAX/MIN/SUM/COUNT` 和对应 `GROUP BY`。一分钟均值示例：

```sql
SELECT time_bucket('1 minute', "timeStamp") AS "timeStamp",
       AVG(value) AS value_avg
FROM "<datasource_identifier>"
WHERE "timeStamp" >= '<start+08:00>'
  AND "timeStamp" < '<end+08:00>'
  AND status = 0
GROUP BY 1
ORDER BY 1
```

- 内部 SQL 含单引号时，嵌入外层前必须转义：

```python
escaped_inner_sql = inner_sql.replace("'", "''")
sql = f"SELECT * FROM ts_query('timescale', '{escaped_inner_sql}')"
```

不能把含时间字面量单引号的 `inner_sql` 原样拼进外层。

## Vxbase

- 物理时间字段为 `ts`。内部 SQL 必须使用 `ts >= "..."`、`ts < "..."`、`ts = "..."` 或 `ts in (...)` 限定时间。
- 内部 `WHERE` 只放 `ts` 时间过滤；`status/name/value` 等非 `ts` 过滤放到 `ts_query(...)` 外层。
- 原始值和 Probe 的内部 SQL 必须 `SELECT *`，不能改成 `SELECT ts, value`。
- 内部不支持 `ORDER BY ts`、`LIMIT ... OFFSET ...` 或 `limit offset,count`。需要限制时只写 `limit N`；执行后再在 DataFrame 按 `ts` 排序。
- 指定时刻没有原始点时，可使用 `interpolation("previous")` 或 `interpolation("linear")`。
- 采样使用 `interval("1m", "10s")`，需要补点时再加 interpolation：

```sql
SELECT * FROM <identifier>
WHERE ts >= "<start+08:00>" AND ts < "<end+08:00>"
interval("1m", "10s") interpolation("previous")
```

- 统计必须把函数写入内部 `SELECT` 并使用 `interval(...)` 定义窗口，不能只写 `SELECT * ... interval(...)`：

```sql
SELECT name, ts, status, avg(value)
FROM <identifier>
WHERE ts >= "<start+08:00>" AND ts < "<end+08:00>"
interval("1m", "10s")
```

- 支持 `max/min/first/last/sum/avg/VAR/count` 以及 vxbase 支持的 event/time 变体；统计查询不再叠加 interpolation。
- `load_data_with_sql(...)` 会把单一聚合表达式列规范为原字段名，例如单独 `avg(value)` 返回 `value`；同一字段多种统计规范为 `value_avg/value_max/...`。后续计算、图表和报告必须使用这些稳定列名，不猜测 `avg` 或直接依赖 `avg(value)`。
- 完整外层示例：

```sql
-- 原始值
SELECT * FROM ts_query('vxbase',
  'SELECT * FROM vxba_xxx WHERE ts >= "2026-05-01T00:00:00+08:00" AND ts < "2026-05-02T00:00:00+08:00" limit 200')
WHERE status = 0

-- 一分钟采样
SELECT * FROM ts_query('vxbase',
  'SELECT * FROM vxba_xxx WHERE ts >= "2026-05-01T00:00:00+08:00" AND ts < "2026-05-02T00:00:00+08:00" interval("1m", "10s") interpolation("previous")')
WHERE status = 0

-- 一分钟均值
SELECT * FROM ts_query('vxbase',
  'SELECT name, ts, status, avg(value) FROM vxba_xxx WHERE ts >= "2026-05-01T00:00:00+08:00" AND ts < "2026-05-02T00:00:00+08:00" interval("1m", "10s")')
```

- `unknown tag/no results` 表示当前时序路径无可查询数据或尚未初始化。不要继续猜表名或方言；多源分析继续保留其他成功路径，只有全部必要路径为空才整体无数据。

## 校验、产物与多源边界

- 每个时序来源都在 `sql_context.fragments` 提供独立片段：

```python
{
    "kind": "time_series_sql",
    "ts_query_source": "vxbase",
    "inner_sql": inner_sql,
    "datasource_identifier": identifier,
    "time_field": "ts",
    "quality_field": "status",
    "value_fields": ["value"],
}
```

- 循环查询多个时序来源时，先构造全部 query plan/fragments，一次校验完整范围，再逐来源执行；不能只校验第一个样例片段。
- 多个彼此独立的时序来源默认分别分析和绘图。只有用户要求对比、同一对象多指标或已有确认可比关系时合并。
- 字符串时序默认在表格和报告展示最新值、枚举、Top 值或样例，不凭空编码成连续趋势；布尔值可按 0/1 展示状态变化。
- 时序表与关系表联合时，先在时序内部缩小时间与粒度，再把 `ts_query(...)` 作为外层子查询按确认关系 JOIN；`sql_context.fragments` 同时包含关系型和时序片段。
- 原始点、采样点和聚合值是不同计算边界。图表、表格和报告说明实际模式、窗口和统计方法。
