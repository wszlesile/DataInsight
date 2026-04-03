# DataInsight 后端接口文档

本文档用于前后端联调，说明当前 Flask 后端实际暴露的 Web 接口、请求参数、响应结构与流式事件格式。

## 1. 文档范围

当前应用真正注册并可访问的接口，以运行时路由表为准，包含：

- `POST /api/agent/invoke`
- `POST /api/agent/stream`
- `GET /api/insight/conversations`
- `PUT /api/insight/conversations/{conversation_id}`
- `GET /api/insight/conversations/{conversation_id}/history`
- `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`
- `GET /api/insight/collects`
- `POST /api/insight/collects`
- `DELETE /api/insight/collects`
- `GET /health`
- `GET /files/{filename}`

说明：

- 代码仓库中还存在其他 controller 文件，但它们目前没有注册到 Flask 应用中，不属于当前对外接口范围。
- 当前 Agent 相关主链路围绕 `conversation -> turn -> message -> execution -> artifact -> memory` 运转。

## 2. 通用响应格式

除 SSE 流式接口外，其余接口统一返回 JSON：

```json
{
  "success": true,
  "data": {},
  "message": "操作成功",
  "code": 200
}
```

字段说明：

- `success`：是否成功
- `data`：业务数据，可能是对象、数组或 `null`
- `message`：提示信息
- `code`：业务状态码

## 3. 认证与用户上下文

当前 Agent、会话、收藏接口中的 `username` 不是由前端直接传入，而是由后端通过当前请求上下文获取。

前端对接时需要注意：

- `POST /api/agent/invoke`
- `POST /api/agent/stream`
- `/api/insight/conversations*`
- `/api/insight/collects*`

这些接口都不要求前端显式传 `username`。

## 4. Agent 分析接口

### 4.1 同步分析

`POST /api/agent/invoke`

用途：

- 发起一次同步分析请求
- 后端会创建或恢复会话、创建轮次、加载当前上下文、调用 Agent 执行分析

请求体：

```json
{
  "namespace_id": 1,
  "conversation_id": 12,
  "user_message": "帮我分析最近三个月销量趋势"
}
```

字段说明：

- `namespace_id`：洞察空间 ID。新建会话时必传；已存在会话时以后端会话归属为准
- `conversation_id`：会话 ID。为空、`0` 或不传时，后端会新建会话
- `user_message`：本轮用户问题

说明：

- 当前轮使用的数据源范围不是由前端直接传入，而是后端根据 `conversation_id` 到会话级数据源绑定关系表中查询得到。
- 后端会在本轮开始时自动刷新会话级 `active_datasource_snapshot`，并把本轮实际数据源范围固化到 `turn` 中。

成功响应示例：

```json
{
  "success": true,
  "message": "操作成功",
  "code": 200,
  "data": {
    "username": "alice",
    "message": "从趋势上看，三个月销量整体先升后降。",
    "conversation_id": 12,
    "turn_id": 35,
    "file_id": "temp/alice_20260403_sales_trend.html",
    "analysis_report": "最近三个月销量在 2 月达到峰值，3 月回落。"
  }
}
```

失败响应示例：

```json
{
  "code": 400,
  "message": "user_message 不能为空"
}
```

### 4.2 流式分析

`POST /api/agent/stream`

用途：

- 发起一次 SSE 流式分析请求
- 前端可以实时接收会话创建、规划说明、工具调用、代码执行进度、最终结果等事件

请求体：

```json
{
  "namespace_id": 1,
  "conversation_id": 12,
  "user_message": "继续按区域维度拆分看一下"
}
```

请求字段与同步分析接口一致。

响应类型：

- `Content-Type: text/event-stream`

每条事件格式：

```text
data: {"type":"session","conversation_id":12,"turn_id":36}

```

### 4.3 SSE 事件类型

当前稳定可用的事件类型包括：

- `session`
- `status`
- `assistant`
- `result`
- `tool_log`
- `done`
- `error`

#### `session`

表示本轮会话上下文已经准备好。

示例：

```json
{
  "type": "session",
  "conversation_id": 12,
  "turn_id": 36,
  "namespace_id": 1,
  "title": "最近三个月销量趋势分析"
}
```

#### `status`

表示流程阶段状态，既包括 Agent 规划阶段，也包括工具执行阶段。

常见 `stage`：

- `start`
- `tool_call`
- `tool_start`
- `tool_running`
- `tool_finished`
- `tool_result`
- `tool_retry`
- `tool_error`
- `tool`

常见 `level`：

- `info`
- `success`
- `warning`
- `error`

示例：

```json
{
  "type": "status",
  "conversation_id": 12,
  "turn_id": 36,
  "stage": "tool_call",
  "level": "info",
  "tool": "execute_python",
  "message": "已生成分析代码，准备执行：销量趋势分析"
}
```

#### `assistant`

表示模型给前端展示的阶段性说明或规划文本。

示例：

```json
{
  "type": "assistant",
  "conversation_id": 12,
  "turn_id": 36,
  "stage": "planning",
  "message": "我会先读取销售数据，再按月份聚合并生成趋势图。"
}
```

#### `result`

表示工具已经返回结构化结果。

示例：

```json
{
  "type": "result",
  "conversation_id": 12,
  "turn_id": 36,
  "stage": "result",
  "file_id": "temp/alice_20260403_sales_trend.html",
  "analysis_report": "最近三个月销量在 2 月达到峰值，3 月回落。"
}
```

#### `tool_log`

表示 `execute_python` 执行过程中产出的日志摘要。

示例：

```json
{
  "type": "tool_log",
  "stage": "tool_output",
  "level": "info",
  "tool": "execute_python",
  "message": "已完成数据聚合，正在生成图表。"
}
```

#### `done`

表示本轮结束。

示例：

```json
{
  "type": "done",
  "conversation_id": 12,
  "turn_id": 36
}
```

#### `error`

表示本轮失败。

示例：

```json
{
  "type": "error",
  "conversation_id": 12,
  "turn_id": 36,
  "stage": "error",
  "level": "error",
  "message": "执行 Python 代码工具错误，请检查后重新生成"
}
```

## 5. 会话接口

### 5.1 获取会话列表

`GET /api/insight/conversations?namespace_id={namespace_id}`

用途：

- 查询当前用户在某个洞察空间下的全部会话
- 用于左侧会话列表或历史页入口

请求参数：

- `namespace_id`：必传，空间 ID

响应示例：

```json
{
  "success": true,
  "code": 200,
  "message": "操作成功",
  "data": [
    {
      "id": 12,
      "username": "alice",
      "insight_namespace_id": 1,
      "title": "最近三个月销量趋势分析",
      "status": "active",
      "summary_text": "第1轮 用户: 帮我分析最近三个月销量趋势；系统结论: 最近三个月销量在 2 月达到峰值，3 月回落",
      "active_datasource_snapshot": "{\"namespace_id\":1,\"conversation_id\":12,\"selected_datasource_ids\":[3,5]}",
      "last_turn_no": 3,
      "last_message_at": "2026-04-03T11:20:30",
      "created_at": "2026-04-03T10:58:10",
      "updated_at": "2026-04-03T11:20:30"
    }
  ]
}
```

### 5.2 重命名会话

`PUT /api/insight/conversations/{conversation_id}`

请求体：

```json
{
  "title": "销量趋势与区域对比分析"
}
```

说明：

- `title` 为空时，后端会按第一轮问题重新生成默认标题

响应示例：

```json
{
  "success": true,
  "code": 200,
  "message": "会话标题已更新",
  "data": {
    "id": 12,
    "title": "销量趋势与区域对比分析"
  }
}
```

### 5.3 获取会话历史时间线

`GET /api/insight/conversations/{conversation_id}/history`

用途：

- 获取某条会话下所有轮次的历史卡片数据
- 用于聊天区历史恢复、侧边栏回放、轮次摘要列表

响应结构：

```json
{
  "success": true,
  "code": 200,
  "message": "操作成功",
  "data": {
    "conversation": {},
    "history": [
      {
        "turn_id": 35,
        "turn_no": 1,
        "question": "帮我分析最近三个月销量趋势",
        "selected_datasource_ids": [3, 5],
        "selected_datasource_snapshot": [
          {
            "datasource_id": 3,
            "datasource_type": "table",
            "datasource_name": "sales_order",
            "datasource_identifier": "sales_order",
            "metadata_schema": {}
          }
        ],
        "report": "最近三个月销量在 2 月达到峰值，3 月回落。",
        "file_id": "temp/alice_20260403_sales_trend.html",
        "latest_execution": {
          "id": 18,
          "turn_id": 35,
          "title": "销量趋势分析",
          "description": "读取销售表并生成趋势图",
          "execution_status": "success",
          "result_file_id": "temp/alice_20260403_sales_trend.html",
          "analysis_report": "最近三个月销量在 2 月达到峰值，3 月回落。",
          "error_message": "",
          "execution_seconds": 1620,
          "finished_at": "2026-04-03T11:05:01"
        },
        "execution_count": 1,
        "status": "success",
        "started_at": "2026-04-03T11:04:58",
        "finished_at": "2026-04-03T11:05:01"
      }
    ]
  }
}
```

说明：

- `history` 接口返回的是轻量轮次列表
- 这里只返回最近执行摘要，不返回完整代码、stdout、stderr
- 完整执行明细请使用轮次详情接口

### 5.4 获取轮次详情

`GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`

用途：

- 获取某一轮的完整详情
- 用于“查看详情”抽屉、执行记录明细、生成代码回放

响应结构：

```json
{
  "success": true,
  "code": 200,
  "message": "操作成功",
  "data": {
    "conversation": {},
    "turn": {
      "id": 35,
      "turn_no": 1,
      "user_query": "帮我分析最近三个月销量趋势",
      "selected_datasource_ids": [3, 5],
      "selected_datasource_snapshot": [],
      "final_answer": "最近三个月销量在 2 月达到峰值，3 月回落。",
      "status": "success"
    },
    "messages": [
      {
        "id": 101,
        "role": "user",
        "message_kind": "prompt",
        "content": "帮我分析最近三个月销量趋势"
      },
      {
        "id": 102,
        "role": "assistant",
        "message_kind": "final_answer",
        "content": "最近三个月销量在 2 月达到峰值，3 月回落。"
      }
    ],
    "executions": [
      {
        "id": 18,
        "conversation_id": 12,
        "turn_id": 35,
        "tool_call_id": "call_xxx",
        "title": "销量趋势分析",
        "description": "读取销售表并生成趋势图",
        "generated_code": "import pandas as pd ...",
        "execution_status": "success",
        "result_file_id": "temp/alice_20260403_sales_trend.html",
        "analysis_report": "最近三个月销量在 2 月达到峰值，3 月回落。",
        "stdout_text": "",
        "stderr_text": "",
        "execution_seconds": 1620,
        "error_message": ""
      }
    ],
    "latest_execution": {},
    "artifacts": [
      {
        "id": 9,
        "artifact_type": "chart",
        "file_id": "temp/alice_20260403_sales_trend.html",
        "summary_text": "最近三个月销量在 2 月达到峰值，3 月回落。"
      }
    ]
  }
}
```

说明：

- `messages` 是当前轮持久化的核心消息
- `executions` 是这一轮完整的代码执行记录
- `artifacts` 是执行后派生产物，比如图表、报告

## 6. 收藏接口

### 6.1 获取收藏列表

`GET /api/insight/collects?namespace_id={namespace_id}`

用途：

- 获取当前用户的收藏列表
- 可按空间过滤

请求参数：

- `namespace_id`：可选。传入后只返回该空间下的收藏

响应示例：

```json
{
  "success": true,
  "code": 200,
  "message": "操作成功",
  "data": [
    {
      "id": 7,
      "username": "alice",
      "collect_type": "turn",
      "target_id": 35,
      "title": "销量趋势分析",
      "summary_text": "最近三个月销量在 2 月达到峰值，3 月回落。",
      "insight_namespace_id": 1,
      "insight_conversation_id": 12,
      "insight_message_id": 0,
      "insight_artifact_id": 9,
      "metadata_json": "{}",
      "created_at": "2026-04-03T11:10:00"
    }
  ]
}
```

### 6.2 新增收藏

`POST /api/insight/collects`

请求体：

```json
{
  "collect_type": "artifact",
  "target_id": 9,
  "title": "销量趋势图",
  "summary_text": "最近三个月销量在 2 月达到峰值，3 月回落。",
  "insight_namespace_id": 1,
  "insight_conversation_id": 12,
  "insight_message_id": 0,
  "insight_artifact_id": 9,
  "metadata_json": {
    "turn_id": 35
  }
}
```

字段说明：

- `collect_type`：收藏类型，当前前端已使用的主要有 `conversation`、`turn`、`artifact`
- `target_id`：收藏目标主键 ID
- `title`：收藏标题
- `summary_text`：收藏摘要
- `insight_namespace_id`：所属空间 ID
- `insight_conversation_id`：所属会话 ID
- `insight_message_id`：所属消息 ID，可选
- `insight_artifact_id`：所属产物 ID，可选
- `metadata_json`：扩展元数据对象

说明：

- 如果同一用户已收藏同一个 `collect_type + target_id`，后端会直接返回已有记录，不会重复创建。

### 6.3 取消收藏

`DELETE /api/insight/collects`

请求体：

```json
{
  "collect_type": "artifact",
  "target_id": 9
}
```

说明：

- 删除为软删除
- 成功时返回“取消收藏成功”

## 7. 健康检查与文件访问

### 7.1 健康检查

`GET /health`

响应示例：

```json
{
  "status": "ok",
  "app": "DataInsight App"
}
```

### 7.2 文件访问

`GET /files/{filename}`

用途：

- 访问分析生成的图表文件、导出文件等

示例：

```text
GET /files/temp/alice_20260403_sales_trend.html
```

说明：

- `file_id` 一般就是传给该接口的文件路径
- 前端可直接将 `file_id` 拼接为 `/files/{file_id}` 进行预览或打开

## 8. 前端对接建议

### 8.1 会话与轮次主链路

建议前端按下面顺序使用接口：

1. 进入页面后，用 `GET /api/insight/conversations?namespace_id=...` 拉取会话列表
2. 选择某条会话后，用 `GET /api/insight/conversations/{conversation_id}/history` 拉取历史轮次
3. 发起分析时，用 `POST /api/agent/stream` 或 `POST /api/agent/invoke`
4. 收到返回中的 `conversation_id` 和 `turn_id` 后，更新当前页面状态
5. 若用户点击“查看详情”，用 `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}` 拉取完整详情

### 8.2 数据源范围

当前设计中：

- 前端不直接把 `selected_datasource_ids` 发给 Agent 分析接口
- 当前轮实际使用的数据源范围，由后端根据当前 `conversation_id` 对应的数据源绑定关系查询得到
- 数据源级唯一标识 `knowledge_tag` 存在于 `insight_datasource` 主表；知识资源级唯一标识 `knowledge_tag` 存在于 `insight_knowledge` 主表，不存在于关系表中

因此前端需要区分两类接口：

- 会话资源配置接口：负责修改会话绑定了哪些数据源
- Agent 分析接口：只负责发起分析，不直接携带当前数据源范围

### 8.3 结果展示

建议前端展示时按三层理解：

- `message`：用户与助手最终对话文本
- `execution`：代码执行记录，是分析逻辑的核心
- `artifact`：图表、报告等派生产物

如果用户要“基于上一轮继续分析”，建议优先参考：

- 最近一轮的 `turn_id`
- 最近一轮的 `selected_datasource_ids`
- 最近一轮的 `latest_execution`
- 最近一轮的 `artifacts`

## 9. 当前未纳入本文档的接口

以下控制器虽然在代码仓库中存在，但当前没有注册到 Flask 应用，不属于目前前端可直接对接的接口：

- `insight_namespace_controller.py`
- `insight_knowledge_controller.py`
- `insight_ns_message_controller.py`
- `insight_ns_rel_datasource_controller.py`
- `insight_ns_rel_knowledge_controller.py`

如果后续这些接口接入应用，需要补充更新本文档。
