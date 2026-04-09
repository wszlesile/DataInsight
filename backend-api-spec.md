# DataInsight 前端对接接口说明

本文档面向前端工程师，只整理**当前前端已经对接并实际使用**的接口。

项目根目录：

- `D:\PycharmProjects\DataInsight`

前端 API 定义文件：

- `D:\PycharmProjects\DataInsight\frontend\src\api\agent.js`

后端路由注册入口：

- `D:\PycharmProjects\DataInsight\src\config\factory.py`

---

## 1. 通用说明

### 1.1 Base URL

前端当前统一通过：

- `/api`

发请求。

例如：

- `/api/agent/stream`
- `/api/insight/namespaces`

### 1.2 普通 JSON 接口响应格式

除 SSE 流接口、PDF 下载接口外，普通接口统一返回：

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
  - 业务数据
- `message`
  - 提示文案
- `code`
  - 业务状态码

失败示例：

```json
{
  "success": false,
  "data": null,
  "message": "数据源不存在",
  "code": 400
}
```

### 1.3 SSE 流接口格式

当前前端流式接口统一使用：

- `fetch + text/event-stream`

后端每条消息格式为：

```text
data: {"type":"status","message":"已收到请求，正在理解分析需求。"}

```

前端解析逻辑在：

- `D:\PycharmProjects\DataInsight\frontend\src\api\agent.js`

---

## 2. 空间与会话

这部分对应前端左侧空间栏、会话列表、新建空间、新建会话、重命名。

### 2.1 获取空间列表

`GET /api/insight/namespaces`

用途：

- 页面初始化时拉取全部空间

响应 `data[]` 字段说明：

- `id`
  - 空间 ID
- `username`
  - 空间所属用户名
- `name`
  - 空间名称
- `created_at`
  - 创建时间，ISO 字符串
- `updated_at`
  - 更新时间，ISO 字符串

响应 `data` 示例：

```json
[
  {
    "id": 7,
    "username": "anonymous",
    "name": "发放大1212",
    "created_at": "2026-04-09T10:30:00",
    "updated_at": "2026-04-09T10:30:00"
  }
]
```

### 2.2 创建空间

`POST /api/insight/namespaces`

请求体：

```json
{
  "name": "报警分析空间"
}
```

请求字段说明：

- `name`
  - 空间名称
  - 必填
  - 前端通常来自“新建洞察”弹窗输入

用途：

- 新建洞察空间
- 后端会同步创建一条默认会话

响应 `data` 结构：

```json
{
  "namespace": {
    "id": 7,
    "name": "报警分析空间"
  },
  "conversation": {
    "id": 19,
    "title": "新建会话"
  }
}
```

响应 `data` 字段说明：

- `namespace`
  - 新建空间对象
- `conversation`
  - 后端同步创建的默认会话对象

`namespace` 关键字段：

- `id`
  - 新建空间 ID
- `name`
  - 新建空间名称

`conversation` 关键字段：

- `id`
  - 默认会话 ID
- `title`
  - 默认会话标题

前端当前行为：

- 创建成功后直接切到返回的默认会话

### 2.3 重命名空间

`PUT /api/insight/namespaces/{namespace_id}`

请求体：

```json
{
  "name": "新的空间名称"
}
```

请求字段说明：

- `name`
  - 新的空间名称
  - 必填

### 2.4 删除空间

`DELETE /api/insight/namespaces/{namespace_id}`

用途：

- 删除空间
- 后端会同步删除该空间下的会话及相关上下文数据

---

## 3. 会话

这部分对应前端空间下的会话列表、创建会话、重命名会话、加载历史。

### 3.1 获取空间下会话列表

`GET /api/insight/conversations?namespace_id={namespace_id}`

用途：

- 切换空间后拉取该空间下的会话列表

查询参数说明：

- `namespace_id`
  - 空间 ID
  - 必填

响应 `data[]` 字段说明：

- `id`
  - 会话 ID
- `title`
  - 会话标题
- `insight_namespace_id`
  - 所属空间 ID
- `status`
  - 当前会话状态，常见为 `active`
- `last_turn_no`
  - 当前会话最新轮次号
- `last_message_at`
  - 最近一次消息时间

响应 `data` 示例：

```json
[
  {
    "id": 19,
    "title": "新建会话",
    "insight_namespace_id": 7,
    "status": "active",
    "last_turn_no": 3,
    "last_message_at": "2026-04-09T13:50:00"
  }
]
```

### 3.2 创建会话

`POST /api/insight/conversations`

请求体：

```json
{
  "namespace_id": 7,
  "title": "新建会话"
}
```

请求字段说明：

- `namespace_id`
  - 目标空间 ID
  - 必填
- `title`
  - 会话标题
  - 可为空；为空时后端会用默认标题

用途：

- 在当前空间下新增一条空会话

### 3.3 重命名会话

`PUT /api/insight/conversations/{conversation_id}`

请求体：

```json
{
  "title": "新的会话标题"
}
```

请求字段说明：

- `title`
  - 新的会话标题
  - 必填

### 3.4 获取会话历史

`GET /api/insight/conversations/{conversation_id}/history`

用途：

- 聊天主区域加载历史轮次

响应 `data` 结构：

```json
{
  "conversation": {
    "id": 19,
    "title": "新建会话"
  },
  "history": [
    {
      "turn_id": 39,
      "turn_no": 4,
      "question": "分析2024年Q4季度的销售趋势",
      "report": "Markdown 报告",
      "charts": [
        {
          "id": 88,
          "title": "2024年Q4销售趋势",
          "summary_text": "展示 2024 年 Q4 各月份销售额变化趋势。",
          "chart_type": "echarts",
          "chart_spec": {},
          "sort_no": 1
        }
      ],
      "tables": [],
      "chart_artifact_id": 88,
      "chart_artifact_ids": [88],
      "selected_datasource_ids": [1],
      "selected_datasource_snapshot": [],
      "latest_execution": {},
      "execution_count": 1,
      "status": "success",
      "started_at": "2026-04-09T13:51:24",
      "finished_at": "2026-04-09T13:56:26"
    }
  ]
}
```

前端当前重点使用字段：

- `history[].question`
- `history[].report`
- `history[].charts`
- `history[].tables`
- `history[].turn_id`
- `history[].chart_artifact_id`
- `history[].chart_artifact_ids`

响应 `conversation` 字段说明：

- `id`
  - 会话 ID
- `title`
  - 会话标题
- `insight_namespace_id`
  - 所属空间 ID

响应 `history[]` 字段说明：

- `turn_id`
  - 轮次 ID
- `turn_no`
  - 轮次序号，从 1 开始递增
- `question`
  - 用户在该轮输入的问题
- `report`
  - 最终分析报告，Markdown 文本
- `charts`
  - 图表列表
- `tables`
  - 表格列表
- `chart_artifact_id`
  - 主图表 artifact ID，收藏图表时常用
- `chart_artifact_ids`
  - 当前轮全部图表 artifact ID 列表
- `selected_datasource_ids`
  - 本轮绑定的数据源 ID 列表
- `selected_datasource_snapshot`
  - 本轮数据源快照
- `latest_execution`
  - 最近一次执行摘要
- `execution_count`
  - 当前轮的执行次数
- `status`
  - 轮次状态，常见值：
    - `running`
    - `success`
    - `failed`
- `started_at`
  - 本轮开始时间
- `finished_at`
  - 本轮结束时间

`charts[]` 字段说明：

- `id`
  - 图表 artifact ID
- `title`
  - 图表标题
- `summary_text`
  - 图表说明
- `chart_type`
  - 当前前端主要使用 `echarts`
- `chart_spec`
  - 前端直接渲染的图表配置对象
- `sort_no`
  - 图表展示顺序

### 3.5 获取单轮详情

`GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`

用途：

- 打开轮次详情抽屉

响应 `data` 结构：

```json
{
  "conversation": {},
  "turn": {},
  "messages": [],
  "executions": [],
  "latest_execution": {},
  "artifacts": []
}
```

响应字段说明：

- `conversation`
  - 当前会话对象
- `turn`
  - 当前轮对象
- `messages`
  - 当前轮消息列表
- `executions`
  - 当前轮执行记录列表
- `latest_execution`
  - 当前轮最新执行记录
- `artifacts`
  - 当前轮产物列表

`turn` 重点字段：

- `id`
  - 轮次 ID
- `turn_no`
  - 轮次序号
- `user_query`
  - 用户问题
- `final_answer`
  - 最终回答文本
- `status`
  - 轮次状态
- `error_message`
  - 失败时的错误信息

`messages[]` 重点字段：

- `id`
  - 消息 ID
- `seq_no`
  - 当前轮内部消息顺序
- `role`
  - `user / assistant / tool`
- `message_kind`
  - 例如 `prompt / final_answer / error`
- `content`
  - 消息正文
- `content_json`
  - 附加结构化内容

`executions[]` 重点字段：

- `id`
  - 执行记录 ID
- `title`
  - 本次执行标题
- `description`
  - 本次执行描述
- `execution_status`
  - 执行状态
- `generated_code`
  - 生成的 Python 代码
- `analysis_report`
  - 本次执行生成的分析报告
- `result_payload_json`
  - 本次执行完整结构化结果
- `error_message`
  - 执行失败时的错误

`artifacts[]` 重点字段：

- `id`
  - 产物 ID
- `artifact_type`
  - `chart / report / table`
- `title`
  - 产物标题
- `summary_text`
  - 产物摘要
- `content_json`
  - 结构化内容

---

## 4. 数据源

这部分对应前端中间“空间数据源”面板。

当前前端逻辑是：

- 主列表只展示**空间级数据源**
- 勾选框表示“是否绑定到当前会话”
- 不再单独展示“当前会话资源列表”
- UNS 资源树通过**本项目后端代理接口**获取，前端不直接跨域请求第三方平台

### 4.0 获取 UNS 树节点

`POST /api/insight/namespaces/{namespace_id}/uns/tree`

用途：

- 前端加载 UNS 资源树
- 由本项目后端代请求第三方平台接口，解决浏览器跨域问题
- 前端只需要关心树节点展示和多选，不需要直接对接第三方域名

请求体：

```json
{
  "parentId": "0",
  "pageNo": 1,
  "pageSize": 100,
  "keyword": "",
  "searchType": 1
}
```

请求字段说明：
- `parentId`
  - 当前要查询的父节点 ID
  - 根节点固定传 `"0"`
- `pageNo`
  - 页码
  - 当前前端固定传 `1`
- `pageSize`
  - 每页条数
  - 当前前端固定传 `100`
- `keyword`
  - 关键字搜索
  - 当前前端默认为空字符串
- `searchType`
  - 搜索类型
  - 当前前端固定传 `1`

响应结构：

```json
{
  "success": true,
  "data": {
    "pageNo": 1,
    "pageSize": 100,
    "total": 55,
    "code": 200,
    "msg": "Simple DB Search",
    "data": [
      {
        "id": "101",
        "alias": "_uns_iiot",
        "name": "IIoT采集设备节点",
        "pathName": "IIoT采集设备节点",
        "hasChildren": true,
        "countChildren": 436,
        "type": 0
      }
    ]
  },
  "message": "操作成功",
  "code": 200
}
```

响应字段说明：
- 顶层 `success / message / code`
  - 本项目统一响应包裹
- 顶层 `data`
  - 第三方 UNS 树接口的原始响应对象
- `data.data`
  - 当前节点列表数组

单个节点字段说明：
- `id`
  - 节点 ID
- `alias`
  - 节点别名
- `name`
  - 节点名称
- `pathName`
  - 节点路径名称
- `hasChildren`
  - 是否有子节点
- `countChildren`
  - 子节点数量
- `type`
  - 节点类型
  - 当前前端会结合 `hasChildren / countChildren / type` 判断它是文件夹还是文件

前端当前用法说明：
- 根节点加载时传 `parentId = "0"`
- 点击文件夹继续展开时，传当前节点的 `id` 作为新的 `parentId`
- 只把已勾选的文件节点 `alias` 数组传给“导入 UNS 节点到空间数据源”接口

### 4.1 获取空间数据源列表

`GET /api/insight/namespaces/{namespace_id}/datasources`

可选查询参数：

- `insight_conversation_id`

示例：

```http
GET /api/insight/namespaces/7/datasources?insight_conversation_id=19
```

用途：

- 拉取当前空间下全部数据源
- 如果传了 `insight_conversation_id`，后端会直接返回每条数据源的 `checked`

响应 `data` 示例：

```json
[
  {
    "id": 14,
    "datasource_id": 14,
    "insight_namespace_id": 7,
    "insight_conversation_id": 19,
    "datasource_type": "local_file",
    "datasource_name": "报警详细查询",
    "knowledge_tag": "upload_xxx",
    "datasource_schema": "{...}",
    "datasource_config_json": "{...}",
    "checked": true,
    "created_at": "2026-04-09T10:40:00",
    "updated_at": "2026-04-09T10:40:00"
  }
]
```

前端当前重点使用字段：

- `datasource_id`
- `datasource_name`
- `datasource_type`
- `datasource_schema`
- `checked`

查询参数说明：

- `insight_conversation_id`
  - 可选
  - 传入后，后端会直接计算当前会话是否已绑定该数据源，并返回 `checked`

响应 `data[]` 字段说明：

- `id`
  - 数据源主键 ID
- `datasource_id`
  - 与 `id` 一致，前端绑定/解绑时一般用这个字段
- `insight_namespace_id`
  - 所属空间 ID
- `insight_conversation_id`
  - 当前查询时带入的会话 ID；未传时一般为 `0`
- `datasource_type`
  - 数据源类型
  - 当前常见值：
    - `local_file`
    - `minio_file`
    - `table`
    - `api`
- `datasource_name`
  - 数据源名称
- `knowledge_tag`
  - 数据源唯一标识或检索标签
- `datasource_schema`
  - 数据源元数据 Schema，字符串形式 JSON
- `datasource_config_json`
  - 数据源配置，字符串形式 JSON
- `checked`
  - 当前会话是否已绑定
- `created_at`
  - 创建时间
- `updated_at`
  - 更新时间

### 4.2 上传空间数据源文件

`POST /api/insight/namespaces/{namespace_id}/datasources/upload`

请求类型：

- `multipart/form-data`

表单字段：

- `file`

表单字段说明：

- `file`
  - 上传文件对象
  - 必填

支持文件：

- `csv`
- `xls`
- `xlsx`

用途：

- 上传文件到当前空间
- 后端创建一条空间级数据源
- **不会自动绑定到当前会话**

响应 `data` 字段说明：

- 返回值就是新建后的数据源对象
- 字段结构与“获取空间数据源列表”中的单条数据源一致

### 4.3 导入 UNS 节点到空间数据源

`POST /api/insight/namespaces/{namespace_id}/datasources/import-uns`

用途：

- 把前端已选中的 UNS 文件节点批量导入为当前空间下的 `table` 类型数据源
- 这里不会自动绑定到当前会话

请求体：

```json
{
  "aliases": [
    "_baojingjilubiao_5d3feea65c1942bdbb7a",
    "_baojingchuzhigongdan_53b31f4fe6c24fd3b8a1"
  ]
}
```

请求字段说明：
- `aliases`
  - UNS 文件节点 alias 数组
  - 只传文件节点，不传文件夹

响应 `data` 结构：

```json
{
  "imported": [
    {
      "datasource_id": 16,
      "datasource_type": "table",
      "datasource_name": "报警记录表",
      "knowledge_tag": "_baojingjilubiao_5d3feea65c1942bdbb7a",
      "datasource_schema": "{...}",
      "datasource_config_json": "{...}",
      "checked": false
    }
  ],
  "failed": [
    {
      "alias": "_xxx",
      "message": "未查询到 UNS 节点详情"
    }
  ]
}
```

响应字段说明：
- `imported`
  - 成功导入的数据源列表
- `failed`
  - 失败的 alias 列表及错误原因

说明：
- 后端会使用当前用户 `UserContext` 中的 token 调第三方详情接口
- 后端会使用当前用户 `UserContext` 中初始化好的 `LakeRDS` 数据库名
- 导入后的数据源类型仍然是 `table`

### 4.4 删除空间数据源

`DELETE /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}`

用途：

- 删除空间级数据源

注意：

- 如果该数据源仍被会话绑定，会删除失败
- 后端会提示先解绑再删除

失败示例：

```json
{
  "success": false,
  "data": null,
  "message": "当前数据源已被 1 个会话引用，请先解绑后再删除",
  "code": 400
}
```

### 4.5 绑定数据源到会话

`POST /api/insight/conversation/datasource/`

请求体：

```json
{
  "insight_conversation_id": 19,
  "datasource_id": 14
}
```

请求字段说明：

- `insight_conversation_id`
  - 当前会话 ID
- `datasource_id`
  - 要绑定的数据源 ID

响应 `data` 字段说明：

- 返回当前绑定关系对应的数据源对象
- 主要用于前端确认绑定成功

用途：

- 空间数据源列表勾选时调用

### 4.6 从会话解绑数据源

`DELETE /api/insight/conversation/datasource/`

请求体：

```json
{
  "insight_conversation_id": 19,
  "datasource_id": 14
}
```

请求字段说明：

- `insight_conversation_id`
  - 当前会话 ID
- `datasource_id`
  - 要解绑的数据源 ID

用途：

- 空间数据源列表取消勾选时调用

---

## 5. 对话分析

这部分对应聊天输入、流式分析、刷新分析。

### 5.1 同步分析

`POST /api/agent/invoke`

请求体：

```json
{
  "namespace_id": 7,
  "conversation_id": 19,
  "user_message": "分析2024年Q4季度的销售趋势"
}
```

请求字段说明：

- `namespace_id`
  - 当前空间 ID
- `conversation_id`
  - 当前会话 ID
  - 可为空；为空时后端会创建新会话
- `user_message`
  - 用户输入内容

当前前端主要走流式接口，这个同步接口仍保留。

### 5.2 流式分析

`POST /api/agent/stream`

请求体：

```json
{
  "namespace_id": 7,
  "conversation_id": 19,
  "user_message": "分析2024年Q4季度的销售趋势"
}
```

请求字段说明：

- `namespace_id`
  - 当前空间 ID
- `conversation_id`
  - 当前会话 ID
- `user_message`
  - 用户输入内容

用途：

- 聊天输入主链路

常见流事件：

- `session`
- `status`
- `assistant`
- `result`
- `done`
- `error`

#### `session`

示例：

```json
{
  "type": "session",
  "conversation_id": 19,
  "turn_id": 39,
  "namespace_id": 7,
  "title": "新建会话"
}
```

字段说明：

- `type`
  - 固定为 `session`
- `conversation_id`
  - 当前会话 ID
- `turn_id`
  - 当前轮次 ID
- `namespace_id`
  - 当前空间 ID
- `title`
  - 当前会话标题

#### `status`

示例：

```json
{
  "type": "status",
  "conversation_id": 19,
  "turn_id": 39,
  "stage": "tool_call",
  "level": "info",
  "message": "已生成分析代码，准备执行：2024年Q4销售趋势分析"
}
```

字段说明：

- `type`
  - 固定为 `status`
- `conversation_id`
  - 当前会话 ID
- `turn_id`
  - 当前轮次 ID
- `stage`
  - 当前阶段，常见值：
    - `start`
    - `rerun`
    - `retry`
    - `tool_call`
    - `tool_start`
    - `tool_running`
    - `tool_finished`
    - `tool_result`
    - `tool_error`
- `level`
  - 当前消息等级，常见值：
    - `info`
    - `success`
    - `warning`
    - `error`
- `message`
  - 前端展示文案
- `tool`
  - 可选；工具阶段时返回工具名，例如 `execute_python`

#### `assistant`

用途：

- 展示中间规划性文本
- 不一定是最终回答

字段说明：

- `type`
  - 固定为 `assistant`
- `conversation_id`
  - 当前会话 ID
- `turn_id`
  - 当前轮次 ID
- `stage`
  - 当前阶段，常见为 `planning`
- `message`
  - 助手当前输出文本

#### `result`

这是前端展示最终结果的关键事件。

示例：

```json
{
  "type": "result",
  "conversation_id": 19,
  "turn_id": 39,
  "stage": "result",
  "analysis_report": "Markdown 报告",
  "charts": [
    {
      "title": "2024年Q4销售趋势",
      "chart_type": "echarts",
      "description": "展示 2024 年 Q4 各月份销售额变化趋势。",
      "chart_spec": {}
    }
  ],
  "tables": [],
  "chart_artifact_id": 88,
  "chart_artifact_ids": [88]
}
```

字段说明：

- `type`
  - 固定为 `result`
- `conversation_id`
  - 当前会话 ID
- `turn_id`
  - 当前轮次 ID
- `stage`
  - 固定为 `result`
- `analysis_report`
  - 最终分析报告，Markdown 文本
- `charts`
  - 图表列表
- `tables`
  - 表格列表
- `chart_artifact_id`
  - 主图表 artifact ID
- `chart_artifact_ids`
  - 全部图表 artifact ID 列表

前端当前主要依赖：

- `analysis_report`
- `charts`
- `tables`
- `chart_artifact_id`
- `chart_artifact_ids`

#### `done`

用途：

- 标记本轮流结束

字段说明：

- `type`
  - 固定为 `done`
- `conversation_id`
  - 当前会话 ID
- `turn_id`
  - 当前轮次 ID

#### `error`

用途：

- 标记本轮执行失败

字段说明：

- `type`
  - 固定为 `error`
- `conversation_id`
  - 当前会话 ID
- `turn_id`
  - 当前轮次 ID
- `stage`
  - 常见为 `error`
- `level`
  - 常见为 `error`
- `message`
  - 错误信息

### 5.3 原轮次刷新分析

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/stream`

用途：

- 在原轮次上重新执行分析
- 不新增新轮次

请求体：

```json
{}
```

说明：

- 前端当前不传额外参数
- 后端会用原轮次的 `user_query` 重新执行分析

返回格式：

- 与 `POST /api/agent/stream` 相同，也是 SSE

说明：

- 当前前端“刷新分析”按钮走的就是这个接口
- 重跑前会清理该轮旧的 assistant 消息、执行记录和产物
- 成功后结果回写到同一个 `turn_id`

---

## 6. 导出

### 6.1 导出当前轮 PDF

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`

用途：

- 导出整轮分析结果 PDF

请求体：

```json
{}
```

字段说明：

- 当前前端传空对象即可
- 后端按会话 ID 和轮次 ID 直接生成 PDF

响应类型：

- `application/pdf`

前端当前使用：

- `responseType = blob`

---

## 7. 收藏

这部分对应右侧“我的收藏”面板和结果卡上的收藏动作。

### 7.1 获取收藏列表

`GET /api/insight/collects?namespace_id={namespace_id}`

用途：

- 拉取当前空间下可见收藏

查询参数说明：

- `namespace_id`
  - 空间 ID
  - 可选；当前前端会传

### 7.2 创建收藏

`POST /api/insight/collects`

常见请求体：

#### 收藏整轮结果

```json
{
  "collect_type": "turn",
  "target_id": 39,
  "title": "分析2024年Q4季度的销售趋势",
  "summary_text": "Markdown 报告",
  "insight_namespace_id": 7,
  "insight_conversation_id": 19,
  "metadata_json": {
    "turn_id": 39,
    "charts": [],
    "tables": []
  }
}
```

请求字段说明：

- `collect_type`
  - 收藏类型
  - 当前前端常用：
    - `conversation`
    - `turn`
    - `artifact`
- `target_id`
  - 收藏目标 ID
- `title`
  - 收藏标题
- `summary_text`
  - 收藏摘要
- `insight_namespace_id`
  - 所属空间 ID
- `insight_conversation_id`
  - 所属会话 ID
- `insight_artifact_id`
  - 可选；收藏图表时会传
- `metadata_json`
  - 扩展元数据

#### 收藏单独图表

```json
{
  "collect_type": "artifact",
  "target_id": 88,
  "title": "2024年Q4销售趋势",
  "summary_text": "",
  "insight_namespace_id": 7,
  "insight_conversation_id": 19,
  "insight_artifact_id": 88,
  "metadata_json": {
    "turn_id": 39,
    "chart_spec": {}
  }
}
```

#### 收藏会话

```json
{
  "collect_type": "conversation",
  "target_id": 19,
  "title": "新建会话",
  "summary_text": "",
  "insight_namespace_id": 7,
  "insight_conversation_id": 19
}
```

### 7.3 取消收藏

`DELETE /api/insight/collects`

请求体示例：

```json
{
  "collect_type": "artifact",
  "target_id": 88
}
```

请求字段说明：

- `collect_type`
  - 收藏类型
- `target_id`
  - 收藏目标 ID

---

## 8. 文件接口

### 8.1 文件访问

`GET /files/{filename}`

当前说明：

- 该路由仍然存在
- 但前端当前主结果展示已经优先使用结构化 `chart_spec`
- 不再把 `/files/...` 作为分析结果主展示链

---

## 9. 当前前端已对接接口清单

对应文件：

- `D:\PycharmProjects\DataInsight\frontend\src\api\agent.js`

已对接方法：

- `invokeAgent`
- `streamAgent`
- `streamRerunTurn`
- `listNamespaces`
- `createNamespace`
- `renameNamespace`
- `deleteNamespace`
- `listConversations`
- `createConversation`
- `renameConversation`
- `getConversationHistory`
- `getTurnDetail`
- `bindConversationDatasource`
- `unbindConversationDatasource`
- `listNamespaceDatasources`
- `uploadNamespaceDatasource`
- `fetchUnsTreeNodes`
- `importNamespaceUnsDatasources`
- `deleteNamespaceDatasource`
- `exportTurnPdf`
- `listCollects`
- `createCollect`
- `removeCollect`

---

## 10. 前端对接建议

### 10.1 页面初始化

1. 调 `GET /api/insight/namespaces`
2. 进入默认空间后调 `GET /api/insight/conversations?namespace_id=...`
3. 选中会话后调 `GET /api/insight/conversations/{conversation_id}/history`
4. 同时调 `GET /api/insight/namespaces/{namespace_id}/datasources?insight_conversation_id=...`

### 10.2 空间数据源面板

1. 主列表使用：
   - `GET /api/insight/namespaces/{namespace_id}/datasources?insight_conversation_id=...`
2. `checked=true`
   - 表示该数据源已绑定当前会话
3. 勾选时：
   - `POST /api/insight/conversation/datasource/`
4. 取消勾选时：
   - `DELETE /api/insight/conversation/datasource/`
5. 上传文件时：
   - `POST /api/insight/namespaces/{namespace_id}/datasources/upload`
6. 加载 UNS 树时：
   - `POST /api/insight/namespaces/{namespace_id}/uns/tree`
7. 从 UNS 节点导入时：
   - `POST /api/insight/namespaces/{namespace_id}/datasources/import-uns`
8. 删除数据源时：
   - `DELETE /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}`

### 10.3 聊天分析

1. 输入消息：
   - `POST /api/agent/stream`
2. 历史结果展示：
   - `GET /api/insight/conversations/{conversation_id}/history`
3. 查看详情：
   - `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`
4. 刷新分析：
   - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/stream`
5. 导出 PDF：
   - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`
