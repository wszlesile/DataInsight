# DataInsight 后端接口说明

本文档用于前后端联调，聚焦两类接口：

- 前端当前已经对接并在页面中实际使用的接口
- 后端当前已经实现并注册到 Flask 应用中的接口

本文档以当前代码为准，接口来源于：

- [D:\PycharmProjects\DataInsight\src\config\factory.py](D:/PycharmProjects/DataInsight/src/config/factory.py)
- [D:\PycharmProjects\DataInsight\frontend\src\api\agent.js](D:/PycharmProjects/DataInsight/frontend/src/api/agent.js)

## 1. 当前接口范围

当前后端实际注册并可访问的接口如下：

- `POST /api/agent/invoke`
- `POST /api/agent/stream`
- `GET /api/insight/namespaces`
- `POST /api/insight/namespaces`
- `PUT /api/insight/namespaces/{namespace_id}`
- `DELETE /api/insight/namespaces/{namespace_id}`
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

- 当前前端主流程实际使用的是 `stream` 流式分析接口。
- `invoke` 同步接口已实现，但当前前端主页面不作为默认分析入口。
- 代码仓库中虽然还有其他 controller 文件，但没有注册到 Flask，不属于当前对外接口范围。

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
- `data`：业务数据，可能为对象、数组或 `null`
- `message`：提示信息
- `code`：业务状态码

常见失败响应：

```json
{
  "success": false,
  "data": null,
  "message": "参数错误",
  "code": 400
}
```

## 3. 鉴权与用户上下文

当前这些接口的 `username` 都由后端从请求上下文中获取，前端不需要显式传入：

- `/api/agent/*`
- `/api/insight/namespaces*`
- `/api/insight/conversations*`
- `/api/insight/collects*`

## 4. 前端当前已对接接口

前端接口定义见 [D:\PycharmProjects\DataInsight\frontend\src\api\agent.js](D:/PycharmProjects/DataInsight/frontend/src/api/agent.js)。

### 4.1 Agent 分析

#### `POST /api/agent/stream`

用途：

- 前端主页面默认分析入口
- 用于实时接收会话创建、过程状态、最终图表与分析报告

请求体：

```json
{
  "namespace_id": 1,
  "conversation_id": 12,
  "user_message": "分析2024年Q4季度的销售趋势",
  "datasource": {}
}
```

字段说明：

- `namespace_id`：当前洞察空间 ID
- `conversation_id`：当前会话 ID；新建空间后前端会直接拿到一条真实会话 ID
- `user_message`：用户本轮输入
- `datasource`：前端当前仍会透传该字段，但分析链路实际以会话绑定资源为准，不依赖它来决定本轮数据源范围

前端当前消费的 SSE 事件类型：

- `session`
- `status`
- `assistant`
- `result`
- `tool_log`
- `done`
- `error`

前端实际消费字段：

- `conversation_id`
- `turn_id`
- `namespace_id`
- `title`
- `stage`
- `level`
- `message`
- `tool`
- `file_id`
- `analysis_report`
- `chart_artifact_id`

#### `POST /api/agent/invoke`

用途：

- 同步分析接口
- 当前后端已实现，前端 API 文件中已保留调用方法

请求体与 `stream` 基本一致：

```json
{
  "namespace_id": 1,
  "conversation_id": 12,
  "user_message": "分析2024年Q4季度的销售趋势"
}
```

成功返回示例：

```json
{
  "success": true,
  "message": "操作成功",
  "code": 200,
  "data": {
    "username": "anonymous",
    "message": "分析已完成",
    "conversation_id": 12,
    "turn_id": 35,
    "file_id": "temp/anonymous_20260404_q4_sales_trend.html",
    "analysis_report": "Q4 销售额整体呈上升趋势。"
  }
}
```

### 4.2 洞察空间

#### `GET /api/insight/namespaces`

用途：

- 拉取当前用户的洞察空间列表

返回字段：

- `id`
- `username`
- `name`
- `is_deleted`
- `created_at`

#### `POST /api/insight/namespaces`

用途：

- 新建洞察空间
- 当前业务设计下，创建空间时会同时创建一条真实会话，前端拿到后可直接发起对话

请求体：

```json
{
  "name": "销售洞察"
}
```

成功返回结构：

```json
{
  "success": true,
  "message": "洞察空间已创建",
  "code": 201,
  "data": {
    "namespace": {
      "id": 3,
      "username": "anonymous",
      "name": "销售洞察",
      "is_deleted": 0,
      "created_at": "2026-04-07T10:20:00"
    },
    "conversation": {
      "id": 18,
      "username": "anonymous",
      "insight_namespace_id": 3,
      "title": "销售洞察",
      "status": "active",
      "summary_text": "",
      "active_datasource_snapshot": "{}",
      "last_turn_no": 0,
      "is_deleted": 0,
      "last_message_at": "2026-04-07T10:20:00",
      "created_at": "2026-04-07T10:20:00",
      "updated_at": "2026-04-07T10:20:00"
    }
  }
}
```

#### `PUT /api/insight/namespaces/{namespace_id}`

用途：

- 重命名洞察空间

请求体：

```json
{
  "name": "销售洞察（Q2）"
}
```

说明：

- 同一用户下空间名称不可重复
- 名称不能为空

#### `DELETE /api/insight/namespaces/{namespace_id}`

用途：

- 删除洞察空间

说明：

- 当前空间与会话是 `1:1`
- 删除空间时，后端会同步软删除该空间下的会话、轮次、消息、执行记录、产物、记忆、收藏及资源关系

### 4.3 会话与历史

#### `GET /api/insight/conversations?namespace_id={namespace_id}`

用途：

- 拉取某个空间下的会话列表

当前返回字段：

- `id`
- `username`
- `insight_namespace_id`
- `title`
- `status`
- `summary_text`
- `active_datasource_snapshot`
- `last_turn_no`
- `is_deleted`
- `last_message_at`
- `created_at`
- `updated_at`

#### `PUT /api/insight/conversations/{conversation_id}`

用途：

- 重命名当前会话标题

请求体：

```json
{
  "title": "Q4 销售趋势分析"
}
```

说明：

- 当 `title` 为空时，后端会根据历史首问自动生成标题

#### `GET /api/insight/conversations/{conversation_id}/history`

用途：

- 拉取某条会话的历史时间线
- 用于聊天区历史恢复

返回结构：

```json
{
  "success": true,
  "data": {
    "conversation": {},
    "history": [
      {
        "turn_id": 35,
        "turn_no": 1,
        "question": "分析2024年Q4季度的销售趋势",
        "selected_datasource_ids": [4, 5, 6],
        "selected_datasource_snapshot": [],
        "report": "Q4 销售额整体呈上升趋势。",
        "file_id": "temp/anonymous_20260404_q4_sales_trend.html",
        "chart_artifact_id": 55,
        "latest_execution": {
          "id": 81,
          "turn_id": 35,
          "title": "Q4 销售趋势分析",
          "description": "按月份统计销售额并生成趋势图",
          "execution_status": "success",
          "result_file_id": "temp/anonymous_20260404_q4_sales_trend.html",
          "analysis_report": "Q4 销售额整体呈上升趋势。",
          "error_message": "",
          "execution_seconds": 1380,
          "finished_at": "2026-04-07T10:32:01"
        },
        "execution_count": 1,
        "status": "success",
        "started_at": "2026-04-07T10:31:58",
        "finished_at": "2026-04-07T10:32:01"
      }
    ]
  }
}
```

说明：

- `report`：历史结果卡显示用的最终报告
- `file_id`：主图表文件路径
- `chart_artifact_id`：主图表产物 ID，用于“收藏图表”
- `latest_execution`：历史卡片中的轻量执行摘要
- `execution_count`：该轮执行次数

#### `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`

用途：

- 拉取某一轮的完整详情
- 用于详情抽屉、执行记录查看、图表收藏补查

返回结构：

```json
{
  "success": true,
  "data": {
    "conversation": {},
    "turn": {},
    "messages": [],
    "executions": [],
    "latest_execution": {},
    "artifacts": []
  }
}
```

关键字段说明：

- `turn.selected_datasource_ids`：该轮实际选中的数据源 ID
- `turn.selected_datasource_snapshot`：该轮数据源快照
- `messages`：该轮持久化的核心消息
- `executions`：该轮完整执行记录列表
- `artifacts`：该轮派生产物列表，当前常见为 `chart` 和 `report`

### 4.4 收藏

#### `GET /api/insight/collects?namespace_id={namespace_id}`

用途：

- 拉取当前空间下的收藏列表

当前前端会统一展示，不再按 tab 分开。

返回字段：

- `id`
- `username`
- `collect_type`
- `target_id`
- `title`
- `summary_text`
- `insight_namespace_id`
- `insight_conversation_id`
- `insight_message_id`
- `insight_context_id`
- `insight_artifact_id`
- `metadata_json`
- `is_deleted`
- `created_at`

#### `POST /api/insight/collects`

用途：

- 创建收藏

当前前端已使用的收藏类型：

- `conversation`：收藏会话
- `turn`：收藏整轮分析结果
- `artifact`：收藏单独图表

请求体示例一：收藏整轮分析结果

```json
{
  "collect_type": "turn",
  "target_id": 35,
  "title": "分析2024年Q4季度的销售趋势",
  "summary_text": "Q4 销售额整体呈上升趋势。",
  "insight_namespace_id": 1,
  "insight_conversation_id": 12,
  "insight_artifact_id": 55,
  "metadata_json": {
    "turn_id": 35,
    "file_id": "temp/anonymous_20260404_q4_sales_trend.html"
  }
}
```

请求体示例二：收藏单独图表

```json
{
  "collect_type": "artifact",
  "target_id": 55,
  "title": "分析2024年Q4季度的销售趋势 图表",
  "summary_text": "",
  "insight_namespace_id": 1,
  "insight_conversation_id": 12,
  "insight_artifact_id": 55,
  "metadata_json": {
    "turn_id": 35,
    "file_id": "temp/anonymous_20260404_q4_sales_trend.html"
  }
}
```

说明：

- 当前图表收藏不再要求展示分析报告，因此 `artifact` 收藏的 `summary_text` 允许为空
- 同一用户对同一个 `collect_type + target_id` 重复收藏时，后端会直接返回已有记录

#### `DELETE /api/insight/collects`

用途：

- 取消收藏

请求体：

```json
{
  "collect_type": "artifact",
  "target_id": 55
}
```

## 5. SSE 事件说明

流式接口 `POST /api/agent/stream` 返回 `text/event-stream`。

每条事件格式：

```text
data: {"type":"session","conversation_id":12,"turn_id":36}

```

### 5.1 `session`

表示本轮会话与轮次已建立。

字段：

- `type`
- `conversation_id`
- `turn_id`
- `namespace_id`
- `title`

### 5.2 `status`

表示过程状态事件。

常见字段：

- `type`
- `conversation_id`
- `turn_id`
- `stage`
- `level`
- `message`
- `tool`

常见 `stage`：

- `start`
- `tool_call`
- `retry`
- `tool`

### 5.3 `assistant`

表示模型的阶段性说明文本。

前端当前会把这类消息放入工作流进度区，而不是直接当最终分析结果。

### 5.4 `result`

表示模型已经返回了结构化分析结果。

当前关键字段：

- `file_id`
- `analysis_report`
- `chart_artifact_id`

说明：

- 分析成功时，前端会把图表和分析报告组合成同一张结果卡
- `chart_artifact_id` 用于“收藏图表”动作

### 5.5 `tool_log`

表示 `execute_python` 工具在执行过程中的附加日志事件。

### 5.6 `done`

表示本轮流式结束。

### 5.7 `error`

表示本轮失败。

当前关键字段：

- `message`
- `conversation_id`
- `turn_id`

## 6. 文件访问接口

### `GET /files/{filename}`

用途：

- 访问分析生成的图表文件
- 前端图表预览直接依赖该接口

前端当前拼接方式：

```text
/files/{encodeURIComponent(file_id)}
```

例如：

```text
/files/temp/anonymous_20260404_q4_sales_trend.html
```

## 7. 当前前端主流程调用顺序

当前页面主链路大致如下：

1. `GET /api/insight/namespaces`
   拉取空间列表
2. `POST /api/insight/namespaces`
   创建空间，同时返回空间和默认会话
3. `GET /api/insight/conversations?namespace_id=...`
   拉取空间下会话
4. `GET /api/insight/conversations/{conversation_id}/history`
   拉取历史轮次
5. `POST /api/agent/stream`
   发起流式分析
6. `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`
   用户查看某轮详情或前端补查图表产物
7. `GET /api/insight/collects?namespace_id=...`
   拉取收藏
8. `POST /api/insight/collects`
   收藏会话、整轮结果或单独图表
9. `DELETE /api/insight/collects`
   取消收藏

## 8. 后端已实现但前端主页面非默认入口

这些接口已实现，但当前前端主页面不是主要依赖它们：

- `POST /api/agent/invoke`
  同步分析接口
- `GET /health`
  服务健康检查

## 9. 不在本文档范围内的内容

以下内容暂不纳入本文档：

- 未注册到 Flask 的 controller
- 知识库与数据源配置管理接口
- 内部 DAO、Service、数据库表结构说明
- Prompt、上下文工程与执行链内部实现细节

如后续把新的 Web 接口注册到 [D:\PycharmProjects\DataInsight\src\config\factory.py](D:/PycharmProjects/DataInsight/src/config/factory.py)，需要同步更新本文档。
