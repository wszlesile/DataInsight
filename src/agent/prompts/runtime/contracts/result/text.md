# result.text

## 输出目标

- 当前调用要求纯文本洞察结果，不生成图表和结构化表格。
- 结果仍应包含真实查询和计算支持的结论、关键数值与业务解释；只有用户要求建议或现有依据支持行动方向时才给建议，不能为了填充结构而补写通用话术。不能退化为字段说明或孤立数字。
- 不使用 Markdown 标题、列表标记、表格、代码块或 HTML。使用自然段组织内容。

## 保存协议

首次 definition 的 `present(results, parameters)` 不生成 text 正文；宿主先保留计算候选，再由 `express` 阶段在模型看到本次事实后撰写文本并保存。以下是历史源码接口：

```python
result = save_analysis_result(output_text=text_result)
```

- `output_text` 必须是已经求值的非空字符串。
- 不传 `analysis_report`、`charts`、`tables` 或 `output_json`。
- Probe 仍使用 `request_retry(...)`，不保存半成品文本。
