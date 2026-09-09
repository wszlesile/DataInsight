# query.api

<!-- runtime-helper-reference -->

- API 数据源使用 `load_data_with_api(endpoint, method="GET", params=None, headers=None, timeout=30)`。
- endpoint、方法和可用参数必须来自当前数据源 Schema/配置，不能猜测接口路径或凭据。
- 只执行只读 GET/查询型请求；分页、时间范围和筛选参数与 `analysis_parameters`、`query_constraints` 保持一致。
- API 不可访问属于 `data_source_unavailable`，不是无数据；成功响应但业务结果为空才进入无数据判断。
- 只把分析所需字段整理为小 DataFrame；不得在报告中暴露 headers、token 或内部 endpoint。
