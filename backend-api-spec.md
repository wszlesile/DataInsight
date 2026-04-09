# DataInsight 后端接口说明

本文档用于前后端联调，按当前代码实际注册的 Flask 接口整理，并对照前端当前已接入的调用方式说明请求参数、返回结构和用途。

项目根目录：

- `D:\PycharmProjects\DataInsight`

## 1. 通用响应格式

除文件下载和 SSE 流接口外，普通 JSON 接口统一返回：

```json
{
  "success": true,
  "data": {},
  "message": "操作成功",
  "code": 200
}
```

字段说明：

- `success`
  - 是否成功
- `data`
  - 业务数据对象、数组或 `null`
- `message`
  - 提示信息
- `code`
  - 业务状态码

失败示例：

```json
{
  "success": false,
  "data": null,
  "message": "缺少必要参数",
  "code": 400
}
```

## 2. Agent 分析接口

### 2.1 同步分析

`POST /api/agent/invoke`

用途：

- 发起一次同步分析请求
- 普通对话和分析型对话都走这条入口

请求体：

```json
{
  "namespace_id": 7,
  "conversation_id": 11,
  "user_message": "分析2024年Q4季度的销售趋势"
}
```

字段说明：

- `namespace_id`
  - 洞察空间 ID
- `conversation_id`
  - 会话 ID；为空或不传时由后端新建会话
- `user_message`
  - 用户输入

成功响应示例：

```json
{
  "success": true,
  "message": "操作成功",
  "code": 200,
  "data": {
    "username": "anonymous",
    "message": "分析已完成",
    "conversation_id": 11,
    "turn_id": 28,
    "analysis_report": "分析报告正文",
    "charts": [
      {
        "title": "Q4销售趋势",
        "chart_type": "echarts",
        "chart_spec": {}
      }
    ],
    "tables": []
  }
}
```

### 2.2 流式分析

`POST /api/agent/stream`

用途：

- 前端聊天主链路的流式分析接口

请求体与同步接口一致。

响应类型：

- `Content-Type: text/event-stream`

SSE 数据格式：

```text
data: {"type":"session","conversation_id":11,"turn_id":28}

```

常见事件类型：

- `session`
  - 返回当前会话和轮次标识
- `status`
  - 运行阶段状态，如开始、重试、工具调用
- `assistant`
  - 助手自然语言中间态消息
- `result`
  - 最终结构化分析结果
- `done`
  - 本轮流结束
- `error`
  - 本轮执行失败

`result` 事件示例：

```json
{
  "type": "result",
  "conversation_id": 11,
  "turn_id": 28,
  "analysis_report": "分析报告正文",
  "charts": [
    {
      "title": "报警趋势图",
      "chart_type": "echarts",
      "chart_spec": {}
    }
  ],
  "tables": []
}
```

## 3. 洞察空间接口

### 3.1 获取空间列表

`GET /api/insight/namespaces`

用途：

- 查询当前用户可见的空间列表

响应示例：

```json
{
  "success": true,
  "data": [
    {
      "id": 7,
      "username": "anonymous",
      "name": "发放大1212",
      "created_at": "2026-04-09T10:30:00"
    }
  ],
  "message": "操作成功",
  "code": 200
}
```

### 3.2 创建空间

`POST /api/insight/namespaces`

用途：

- 创建空间，并同步创建一条默认会话

请求体：

```json
{
  "name": "报警分析空间"
}
```

响应 `data` 包含：

- `namespace`
- `conversation`

### 3.3 重命名空间

`PUT /api/insight/namespaces/{namespace_id}`

请求体：

```json
{
  "name": "新的空间名称"
}
```

### 3.4 删除空间

`DELETE /api/insight/namespaces/{namespace_id}`

说明：

- 删除空间时，会同步软删除该空间下的会话、轮次、消息、执行、产物、记忆、绑定关系和收藏

## 4. 空间数据源接口

### 4.1 查询空间数据源列表

`GET /api/insight/namespaces/{namespace_id}/datasources`

用途：

- 查询空间下的全部数据源
- 可选地带出当前会话的绑定状态

查询参数：

- `insight_conversation_id`
  - 可选；传入后，后端会在每条数据源上返回 `checked`

示例：

```http
GET /api/insight/namespaces/7/datasources?insight_conversation_id=11
```

响应示例：

```json
{
  "success": true,
  "data": [
    {
      "id": 14,
      "datasource_id": 14,
      "insight_namespace_id": 7,
      "insight_conversation_id": 11,
      "datasource_type": "local_file",
      "datasource_name": "报警详细查询",
      "knowledge_tag": "upload_xxx",
      "datasource_schema": "{...}",
      "datasource_config_json": "{...}",
      "checked": true,
      "created_at": "2026-04-09T10:40:00",
      "updated_at": "2026-04-09T10:40:00"
    }
  ],
  "message": "操作成功",
  "code": 200
}
```

当前前端数据源主列表就是依赖这一个接口展示，并直接使用 `checked` 驱动勾选状态。

### 4.2 上传空间数据源文件

`POST /api/insight/namespaces/{namespace_id}/datasources/upload`

用途：

- 上传 `csv / xls / xlsx`
- 文件保存到 `UPLOAD_DIR`
- 后端自动创建一条空间级 `insight_datasource`

请求类型：

- `multipart/form-data`

表单字段：

- `file`
  - 上传文件

说明：

- 上传只创建空间级数据源
- 不会自动绑定到当前会话

### 4.3 删除空间数据源

`DELETE /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}`

用途：

- 删除空间级数据源定义

删除规则：

- 如果该数据源仍被任意会话引用，则删除失败
- 需要先解除会话绑定，再删除数据源

失败示例：

```json
{
  "success": false,
  "data": null,
  "message": "当前数据源已被 1 个会话引用，请先解绑后再删除",
  "code": 400
}
```

## 5. 会话数据源绑定接口

### 5.1 查询会话已绑定数据源

`GET /api/insight/conversation/datasource/?insight_conversation_id={conversation_id}`

用途：

- 查询某个会话已绑定的数据源关系
- 当前前端主列表不再依赖它计算勾选状态，但接口仍保留

### 5.2 绑定数据源到会话

`POST /api/insight/conversation/datasource/`

请求体：

```json
{
  "insight_conversation_id": 11,
  "datasource_id": 14
}
```

说明：

- 绑定的是“空间级数据源”和“会话”的关系
- 不会改动数据源主表

### 5.3 解绑会话数据源

`DELETE /api/insight/conversation/datasource/`

请求体：

```json
{
  "insight_conversation_id": 11,
  "datasource_id": 14
}
```

说明：

- 只删除绑定关系
- 不删除空间级数据源本体

## 6. 会话接口

### 6.1 查询会话列表

`GET /api/insight/conversations?namespace_id={namespace_id}`

用途：

- 查询某个空间下的会话列表

### 6.2 创建会话

`POST /api/insight/conversations`

用途：

- 在当前空间下创建一条新的空会话

请求体：

```json
{
  "namespace_id": 7,
  "title": "新建会话"
}
```

### 6.3 重命名会话

`PUT /api/insight/conversations/{conversation_id}`

请求体：

```json
{
  "title": "新的会话标题"
}
```

### 6.4 查询会话历史

`GET /api/insight/conversations/{conversation_id}/history`

用途：

- 查询某个会话的轮次历史
- 前端聊天区历史结果卡使用

返回内容主要包括：

- `conversation`
- `history`
  - `turn_id`
  - `turn_no`
  - `question`
  - `report`
  - `charts`
  - `tables`
  - `selected_datasource_ids`
  - `selected_datasource_snapshot`
  - `latest_execution`
  - `execution_count`
  - `status`

### 6.5 查询单轮详情

`GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`

用途：

- 查询单轮完整详情
- 前端详情抽屉使用

返回内容主要包括：

- `conversation`
- `turn`
- `messages`
- `executions`
- `latest_execution`
- `artifacts`

### 6.6 导出单轮 PDF

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`

用途：

- 导出单轮分析结果为 PDF

响应类型：

- `application/pdf`

### 6.7 原轮次重跑

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/stream`

用途：

- 在原 `turn` 上重跑分析
- 不新增新轮次
- 返回 SSE 流事件

说明：

- 重跑前会清理该轮旧的 assistant 消息、执行记录和产物
- 最终结果回写到同一个 `turn_id`

## 7. 收藏接口

### 7.1 查询收藏

`GET /api/insight/collects?namespace_id={namespace_id}`

用途：

- 查询当前用户收藏
- 可按空间过滤

### 7.2 创建收藏

`POST /api/insight/collects`

请求体示例：

```json
{
  "collect_type": "turn",
  "target_id": 28,
  "title": "Q4销售趋势分析",
  "summary_text": "分析报告摘要",
  "insight_namespace_id": 7,
  "insight_conversation_id": 11,
  "insight_message_id": 0,
  "insight_artifact_id": 21,
  "metadata_json": {
    "turn_id": 28
  }
}
```

常见 `collect_type`：

- `conversation`
- `turn`
- `artifact`

### 7.3 取消收藏

`DELETE /api/insight/collects`

请求体：

```json
{
  "collect_type": "artifact",
  "target_id": 21
}
```

## 8. 其他接口

### 8.1 健康检查

`GET /health`

响应示例：

```json
{
  "status": "ok",
  "app": "DataInsight App"
}
```

### 8.2 文件访问

`GET /files/{filename}`

用途：

- 提供本地文件访问能力
- 当前仍保留该路由，但前端分析结果主展示已优先使用结构化图表数据

## 9. 前端当前已对接接口

前端 `frontend/src/api/agent.js` 当前已接入：

- Agent
  - `invokeAgent`
  - `streamAgent`
  - `streamRerunTurn`
- 空间
  - `listNamespaces`
  - `createNamespace`
  - `renameNamespace`
  - `deleteNamespace`
- 会话
  - `listConversations`
  - `createConversation`
  - `renameConversation`
  - `getConversationHistory`
  - `getTurnDetail`
- 数据源
  - `listNamespaceDatasources`
  - `uploadNamespaceDatasource`
  - `deleteNamespaceDatasource`
  - `bindConversationDatasource`
  - `unbindConversationDatasource`
- 收藏
  - `listCollects`
  - `createCollect`
  - `removeCollect`
- 导出
  - `exportTurnPdf`

## 10. 当前推荐对接流程

### 10.1 空间与会话

1. 先拉空间列表
2. 进入空间后拉会话列表
3. 选中会话后拉历史
4. 需要新会话时调用 `POST /api/insight/conversations`

### 10.2 数据源

1. 展示空间数据源：
   - `GET /api/insight/namespaces/{namespace_id}/datasources?insight_conversation_id=...`
2. 上传新文件数据源：
   - `POST /api/insight/namespaces/{namespace_id}/datasources/upload`
3. 勾选绑定当前会话：
   - `POST /api/insight/conversation/datasource/`
4. 取消勾选解绑：
   - `DELETE /api/insight/conversation/datasource/`
5. 删除空间级数据源：
   - `DELETE /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}`

### 10.3 分析

1. 正常聊天/分析：
   - `POST /api/agent/stream`
2. 查询历史：
   - `GET /api/insight/conversations/{conversation_id}/history`
3. 查看详情：
   - `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`
4. 原轮次刷新分析：
   - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/stream`
5. 导出 PDF：
   - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`
