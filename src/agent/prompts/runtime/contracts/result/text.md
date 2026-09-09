# result.text

## 输出目标

- 当前调用要求纯文本洞察结果，不生成图表和结构化表格。
- 结果仍应包含真实查询和计算支持的结论、关键数值、业务解释及必要建议；不能退化为字段说明或孤立数字。
- 不使用 Markdown 标题、列表标记、表格、代码块或 HTML。使用自然段组织内容。

## 保存协议

正式分析代码最终调用：

```python
result = save_analysis_result(output_text=text_result)
```

- `output_text` 必须是已经求值的非空字符串。
- 不传 `analysis_report`、`charts`、`tables` 或 `output_json`。
- Probe 仍使用 `request_retry(...)`，不保存半成品文本。
