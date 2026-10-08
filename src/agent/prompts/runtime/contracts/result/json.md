# result.json

## 输出目标

- 当前调用要求严格 JSON 对象。下方 `<analysis-output-contract>` 是调用方提供的权威输出 Schema。
- 字段名、类型、必填项和字段 `description` 共同定义调用方要求的事实与指标。只能由真实查询和计算填充，不能编造值、静默省略必填字段或增加 Schema 禁止的字段。
- Schema 是结果契约，不是静态答案模板。先在一个完整洞察语义中理解问题并计算，再构造符合契约的对象。
- 无法可靠计算的必填字段：Schema 允许 `null` 时返回 `null`；不允许 `null` 时让本次执行以输出契约不满足失败，不能用 0、空字符串或猜测值代替。

## 保存协议

首次 definition 的 `present(results, parameters)` 不生成 JSON 正文；宿主先保留计算候选，再由 `express` 阶段在模型看到本次事实后构造 JSON 并保存。以下是历史源码接口：

```python
result = save_analysis_result(output_json=result_object)
```

- `output_json` 必须是 Python `dict`，不能是 JSON 字符串、Markdown 代码块或 Pydantic 对象。
- 不传 `analysis_report`、`charts`、`tables` 或 `output_text`。
- `compose_analysis_presentation` 会使用当前调用的 JSON Schema 做确定性结构校验。
- Probe 仍使用 `request_retry(...)`，不保存半成品 JSON。
