# result.visualizations

## 当前产物责任

正式 definition 的 `present(results, parameters)` 读取本次已执行结果并返回完整 `{"charts": ..., "tables": ...}`。宿主保存候选，随后由 express 基于同一结果撰写报告并定稿。这里负责图表和表格，不另写报告或调用保存接口。保留问题所需的全部产物、指标、展示范围和实际来源；确无可视化意义时可以只交付表格。

## 产物 API

<!-- runtime-helper-reference -->

<!-- runtime-artifact-sections -->
