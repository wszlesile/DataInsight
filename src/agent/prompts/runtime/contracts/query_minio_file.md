# query.minio_file

<!-- runtime-helper-reference -->

- MinIO 对象文件使用 `load_minio_file(...)`，bucket、object_name 和 sheet_name 必须来自当前数据源配置，不能猜测。
- 加载后字段严格来自完整 Schema；先裁剪必要列，再做过滤、聚合和有限明细展示。
- Excel 对象带工作表时显式传 `sheet_name`。对象不可访问属于 `data_source_unavailable`，不是无数据。
- 多对象文件业务独立则分别分析；存在已确认关系时先在各自粒度过滤/聚合，再组合小结果。
