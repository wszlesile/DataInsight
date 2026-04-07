# DataInsight 后端接口文档

本文档用于前后端联调，整理当前项目中：

- 后端已经实现并注册到 Flask 的接口
- 前端当前已经对接并实际使用的接口

文档以当前代码为准，工作区路径：`D:\PycharmProjects\DataInsight`

## 1. 接口范围

当前 Flask 应用实际注册的接口主要包括：

- Agent 分析
  - `POST /api/agent/invoke`
  - `POST /api/agent/stream`
- 洞察空间
  - `GET /api/insight/namespaces`
  - `POST /api/insight/namespaces`
  - `PUT /api/insight/namespaces/{namespace_id}`
  - `DELETE /api/insight/namespaces/{namespace_id}`
  - `GET /api/insight/namespaces/{namespace_id}/datasources`
  - `POST /api/insight/namespaces/{namespace_id}/datasources/upload`
- 会话
  - `GET /api/insight/conversations`
  - `PUT /api/insight/conversations/{conversation_id}`
  - `GET /api/insight/conversations/{conversation_id}/history`
  - `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`
  - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`
  - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/stream`
- 会话数据源绑定
  - `GET /api/insight/conversation/datasource/`
  - `POST /api/insight/conversation/datasource/`
  - `DELETE /api/insight/conversation/datasource/`
- 收藏
  - `GET /api/insight/collects`
  - `POST /api/insight/collects`
  - `DELETE /api/insight/collects`
- 其他
  - `GET /health`
  - `GET /files/{filename}`

## 2. 通用响应格式

除文件下载接口和 SSE 接口外，普通 JSON 接口统一返回：

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

失败示例：

```json
{
  "success": false,
  "data": null,
  "message": "缺少必要参数",
  "code": 400
}
```

## 3. Agent 分析接口

### 3.1 同步分析

`POST /api/agent/invoke`

用途：

- 发起一次同步分析请求
- 普通对话和分析型对话都走这一入口

请求体：

```json
{
  "namespace_id": 12,
  "conversation_id": 71,
  "user_message": "分析2024年Q4季度的销售趋势"
}
```

字段说明：

- `namespace_id`：洞察空间 ID。新建会话时需要传入
- `conversation_id`：会话 ID。传空或不传时，后端可新建会话
- `user_message`：用户输入

成功响应示例：

```json
{
  "success": true,
  "code": 200,
  "message": "操作成功",
  "data": {
    "username": "anonymous",
    "message": "Q4 销售额整体逐月上升。",
    "conversation_id": 71,
    "turn_id": 130,
    "file_id": "D:/PycharmProjects/DataInsight/temp/anonymous_xxx.html",
    "analysis_report": "本次分析显示 10 月到 12 月销售额持续增长。"
  }
}
```

说明：

- 如果本轮是普通问答，不一定会返回图表文件
- 如果本轮是分析型请求，成功时通常会返回 `file_id` 和 `analysis_report`

### 3.2 流式分析

`POST /api/agent/stream`

用途：

- 发起一次 SSE 流式分析
- 前端当前聊天主链路使用这个接口

请求体与同步接口一致：

```json
{
  "namespace_id": 12,
  "conversation_id": 71,
  "user_message": "继续分析一下上个月30号报警记录"
}
```

响应类型：

- `Content-Type: text/event-stream`

SSE 事件格式：

```text
data: {"type":"session","conversation_id":71,"turn_id":130}

```

常见事件类型：

- `session`
- `status`
- `assistant`
- `tool_log`
- `result`
- `done`
- `error`

`result` 事件示例：

```json
{
  "type": "result",
  "conversation_id": 71,
  "turn_id": 130,
  "file_id": "D:/PycharmProjects/DataInsight/temp/anonymous_xxx.html",
  "analysis_report": "分析报告正文"
}
```

## 4. 洞察空间接口

### 4.1 获取空间列表

`GET /api/insight/namespaces`

用途：

- 获取当前用户的空间列表
- 前端左侧空间栏使用

响应示例：

```json
{
  "success": true,
  "data": [
    {
      "id": 12,
      "name": "会话11",
      "username": "anonymous",
      "created_at": "2026-04-04T15:56:13.363044"
    }
  ],
  "message": "操作成功",
  "code": 200
}
```

### 4.2 新建空间

`POST /api/insight/namespaces`

用途：

- 新建洞察空间
- 当前实现会同时创建空间和一条默认会话

请求体：

```json
{
  "name": "报警分析空间"
}
```

响应 `data` 中会包含：

- 新空间信息
- 默认创建的会话信息

### 4.3 重命名空间

`PUT /api/insight/namespaces/{namespace_id}`

请求体：

```json
{
  "name": "新的空间名称"
}
```

### 4.4 删除空间

`DELETE /api/insight/namespaces/{namespace_id}`

说明：

- 当前业务是空间和会话 1:1
- 删除空间时，会话及相关上下文数据会一并删除

## 5. 空间数据源接口

### 5.1 获取空间数据源列表

`GET /api/insight/namespaces/{namespace_id}/datasources`

用途：

- 获取当前空间下的全部数据源
- 可选地按当前会话返回勾选状态

查询参数：

- `insight_conversation_id`：可选。传入后，后端会在返回的每条数据源上补 `checked`

示例：

```http
GET /api/insight/namespaces/12/datasources?insight_conversation_id=71
```

响应示例：

```json
{
  "success": true,
  "data": [
    {
      "id": 10,
      "datasource_id": 10,
      "insight_namespace_id": 12,
      "insight_conversation_id": 71,
      "datasource_type": "local_file",
      "datasource_name": "ui_bind_test3",
      "knowledge_tag": "upload_d9d12ff375ee40e5",
      "datasource_schema": "{...}",
      "datasource_config_json": "{...}",
      "checked": true,
      "created_at": "2026-04-07T17:04:15.642501",
      "updated_at": "2026-04-07T17:04:15.642501"
    }
  ],
  "message": "操作成功",
  "code": 200
}
```

当前前端数据源面板已经只使用这一个列表接口来展示勾选状态。

### 5.2 上传空间数据源文件

`POST /api/insight/namespaces/{namespace_id}/datasources/upload`

用途：

- 上传 `csv / xls / xlsx`
- 文件保存到 `UPLOAD_DIR`
- 后端自动生成空间级 `insight_datasource`

请求类型：

- `multipart/form-data`

表单字段：

- `file`：上传文件

说明：

- 上传只创建空间级数据源
- 不会自动绑定到当前会话
- 会话绑定需要单独调用绑定接口

## 6. 会话数据源绑定接口

### 6.1 获取会话已绑定数据源

`GET /api/insight/conversation/datasource/?insight_conversation_id={conversation_id}`

用途：

- 获取某个会话当前已绑定的数据源关系
- 当前前端主列表已不依赖这个接口做勾选状态计算，但接口仍保留

### 6.2 绑定数据源到当前会话

`POST /api/insight/conversation/datasource/`

请求体：

```json
{
  "insight_conversation_id": 71,
  "datasource_id": 10
}
```

说明：

- 绑定的是“会话”和“空间数据源”的关系
- 不会修改空间级数据源主表

### 6.3 从当前会话解绑数据源

`DELETE /api/insight/conversation/datasource/`

请求体：

```json
{
  "insight_conversation_id": 71,
  "datasource_id": 10
}
```

说明：

- 只删除绑定关系
- 不会删除 `insight_datasource` 主表记录

## 7. 会话接口

### 7.1 获取会话列表

`GET /api/insight/conversations?namespace_id={namespace_id}`

用途：

- 获取当前空间下的会话列表

### 7.2 重命名会话

`PUT /api/insight/conversations/{conversation_id}`

请求体：

```json
{
  "title": "新的会话标题"
}
```

### 7.3 获取会话历史

`GET /api/insight/conversations/{conversation_id}/history`

用途：

- 获取某个会话的历史轮次卡片
- 前端聊天区历史展示使用

返回内容主要包括：

- `conversation`
- `history`
  - `turn_id`
  - `turn_no`
  - `question`
  - `report`
  - `file_id`
  - `selected_datasource_ids`
  - `selected_datasource_snapshot`
  - `latest_execution`
  - `execution_count`

### 7.4 获取轮次详情

`GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`

用途：

- 获取某个轮次的完整详情
- 前端“查看详情”抽屉使用

返回内容主要包括：

- `conversation`
- `turn`
- `messages`
- `executions`
- `latest_execution`
- `artifacts`

### 7.5 导出整轮分析结果 PDF

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`

用途：

- 导出某个轮次的完整分析结果 PDF
- 当前 PDF 由后端生成

响应类型：

- `application/pdf`

前端当前通过 `blob` 方式下载。

### 7.6 原轮次重新执行分析

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/stream`

用途：

- 在原轮次上重新执行分析
- 不新增新的对话轮次
- 会覆盖原轮次的最新执行结果和产物

说明：

- 响应为 SSE 流
- 事件类型与 `/api/agent/stream` 基本一致

## 8. 收藏接口

### 8.1 获取收藏列表

`GET /api/insight/collects?namespace_id={namespace_id}`

用途：

- 获取当前用户收藏
- 可按空间过滤

### 8.2 新增收藏

`POST /api/insight/collects`

请求体示例：

```json
{
  "collect_type": "turn",
  "target_id": 129,
  "title": "帮我分析下3月30号的报警数据",
  "summary_text": "2026年3月30日报警数据分析报告",
  "insight_namespace_id": 12,
  "insight_conversation_id": 71,
  "insight_message_id": 0,
  "insight_artifact_id": 61,
  "metadata_json": {
    "turn_id": 129,
    "file_id": "D:/PycharmProjects/DataInsight/temp/anonymous_xxx.html"
  }
}
```

常用 `collect_type`：

- `conversation`
- `turn`
- `artifact`

### 8.3 删除收藏

`DELETE /api/insight/collects`

请求体：

```json
{
  "collect_type": "artifact",
  "target_id": 61
}
```

## 9. 文件与健康检查

### 9.1 健康检查

`GET /health`

响应示例：

```json
{
  "status": "ok",
  "app": "DataInsight App"
}
```

### 9.2 文件访问

`GET /files/{filename}`

用途：

- 打开图表 HTML
- 打开分析产物文件

示例：

```http
GET /files/D:/PycharmProjects/DataInsight/temp/anonymous_xxx.html
```

## 10. 前端当前实际调用链

当前前端 `frontend/src/api/agent.js` 已对接的主要接口如下：

- 空间
  - `listNamespaces`
  - `createNamespace`
  - `renameNamespace`
  - `deleteNamespace`
- 会话
  - `listConversations`
  - `renameConversation`
  - `getConversationHistory`
  - `getTurnDetail`
- Agent
  - `invokeAgent`
  - `streamAgent`
  - `streamRerunTurn`
- 数据源
  - `listNamespaceDatasources`
  - `uploadNamespaceDatasource`
  - `bindConversationDatasource`
  - `unbindConversationDatasource`
- 收藏
  - `listCollects`
  - `createCollect`
  - `removeCollect`
- 导出
  - `exportTurnPdf`

## 11. 当前推荐对接方式

### 11.1 聊天分析主链

1. 先拉空间列表
2. 进入某个空间后拉会话列表
3. 选中会话后拉历史
4. 发送问题时走 `POST /api/agent/stream`
5. 查看某轮详情时走 `GET /turns/{turn_id}`
6. 需要重跑某轮时走 `POST /turns/{turn_id}/rerun/stream`

### 11.2 数据源主链

1. 展示空间数据源：
   - `GET /api/insight/namespaces/{namespace_id}/datasources?insight_conversation_id=...`
2. 上传新文件数据源：
   - `POST /api/insight/namespaces/{namespace_id}/datasources/upload`
3. 绑定到当前会话：
   - `POST /api/insight/conversation/datasource/`
4. 解绑当前会话：
   - `DELETE /api/insight/conversation/datasource/`

关键点：

- 前端主列表展示的是空间数据源
- 是否勾选由同一个空间数据源列表接口直接返回 `checked`
- 前端不需要再自己拼“空间数据源列表 + 会话绑定列表”来判断勾选
