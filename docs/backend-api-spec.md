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
- `/api/agent/tasks`
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

### 3.4 删除会话

`DELETE /api/insight/conversations/{conversation_id}`

用途：

- 删除当前用户下的指定会话
- 后端会同步软删除该会话下的历史轮次、消息、执行记录、分析产物、会话记忆、会话数据源绑定、会话知识绑定和关联收藏
- 不会删除洞察空间本身，也不会删除空间级数据源定义

请求字段说明：

- `conversation_id`
  - 路径参数
  - 要删除的会话 ID

响应示例：

```json
{
  "success": true,
  "message": "会话已删除",
  "data": null
}
```

### 3.5 获取会话提示词

`GET /api/insight/conversations/{conversation_id}/prompt?namespace_id={namespace_id}`

用途：

- 获取当前会话绑定的补充系统提示词
- 该提示词会在分析时追加到基础系统提示词后，不参与历史上下文压缩

查询参数说明：

- `namespace_id`
  - 空间 ID
  - 必填

响应 `data` 示例：

```json
{
  "id": 12,
  "insight_namespace_id": 7,
  "insight_conversation_id": 19,
  "prompt_content": "净效率 = 有效产出 / 实际运行时长。",
  "is_enabled": 1,
  "is_deleted": 0,
  "created_at": "2026-06-03T10:20:00",
  "updated_at": "2026-06-03T10:20:00"
}
```

### 3.6 保存会话提示词

`PUT /api/insight/conversations/{conversation_id}/prompt`

请求体：

```json
{
  "namespace_id": 7,
  "prompt_content": "净效率 = 有效产出 / 实际运行时长。"
}
```

请求字段说明：

- `namespace_id`
  - 空间 ID
  - 必填
- `prompt_content`
  - 当前会话的补充系统提示词，纯文本
  - 最大长度 20000 字

### 3.7 清空会话提示词

`DELETE /api/insight/conversations/{conversation_id}/prompt`

请求体：

```json
{
  "namespace_id": 7
}
```

用途：

- 清空当前会话绑定的补充系统提示词
- 清空后新一轮分析不再注入该补充提示词

### 3.8 获取会话历史

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

### 3.9 获取单轮详情

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
- 前端勾选状态变化后，直接提交“当前整棵树的已选节点集合”给后端统一同步

### 4.0.1 获取当前会话的 UNS 已选节点

`GET /api/insight/namespaces/{namespace_id}/uns/selections?insight_conversation_id={conversation_id}`

用途：

- 获取当前会话已经选择的 UNS 树节点
- 用于前端 UNS 树勾选状态与半选状态回显

查询参数说明：
- `insight_conversation_id`
  - 当前会话 ID
  - 必填

响应 `data` 示例：

```json
[
  {
    "id": 12,
    "insight_namespace_id": 7,
    "insight_conversation_id": 19,
    "uns_node_id": "2035975648114712576",
    "uns_node_name": "报警记录表",
    "uns_node_path": ["101"],
    "is_folder": false,
    "expanded_uns_node_ids": ["2035975648114712576"],
    "selection_state": "selected",
    "derived": false,
    "created_at": "2026-04-12T10:20:00",
    "updated_at": "2026-04-12T10:20:00"
  },
  {
    "id": "partial:101",
    "insight_namespace_id": 7,
    "insight_conversation_id": 19,
    "uns_node_id": "101",
    "uns_node_name": "",
    "uns_node_path": [],
    "is_folder": true,
    "expanded_uns_node_ids": [],
    "selection_state": "partial",
    "derived": true,
    "created_at": "2026-04-12T10:20:00",
    "updated_at": "2026-04-12T10:20:00"
  }
]
```

响应字段说明：
- `uns_node_id`
  - 当前回显节点 ID
- `uns_node_name`
  - 节点名称
- `uns_node_path`
  - 当前节点的祖先节点 ID 数组
  - 字段名历史保留，不再表示路径字符串
- `is_folder`
  - `true` 表示文件夹节点，`false` 表示文件节点
- `expanded_uns_node_ids`
  - 如果当时勾选的是文件夹，这里记录该文件夹展开后命中的文件节点 ID 列表
- `selection_state`
  - `selected` 表示真实选中节点
  - `partial` 表示由子节点带出的半选父节点，仅用于前端展示
- `derived`
  - `true` 表示后端推导出的半选节点
  - `false` 表示用户真实选中的节点

### 4.1 获取空间数据源列表

`GET /api/insight/namespaces/{namespace_id}/datasources`

可选查询参数：

- `insight_conversation_id`

说明：

- 该接口只返回“当前空间自有数据源”
- 通过 UNS 选择树导入并落在共享空间的 table 数据源，不在这里展示
- UNS 资源的选择、回显与绑定状态，请使用：
  - `GET /api/insight/namespaces/{namespace_id}/uns/selections`
  - `POST /api/insight/namespaces/{namespace_id}/datasources/import-uns`

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

- 把前端当前“全量已选”的 UNS 节点集合统一同步为可分析的数据源
- 导入后的 UNS 数据源本体会落到共享空间，避免不同空间重复导入同一份 UNS 定义
- 本次请求会同时完成：
  - 导入/复用共享数据源
  - 同步当前会话的数据源绑定
  - 覆盖更新当前会话的 UNS 选择状态

请求体：

```json
{
  "insight_conversation_id": 19,
  "nodes": [
    {
      "id": "101",
      "alias": "_uns_abi_test",
      "name": "abi测试",
      "unsNodePath": [],
      "hasChildren": true,
      "countChildren": 2,
      "type": 0,
      "pathType": 0,
      "isFolder": true
    },
    {
      "id": "2035975648114712576",
      "alias": "_baojingjilubiao_5d3feea65c1942bdbb7a",
      "name": "报警记录表",
      "unsNodePath": ["101"],
      "hasChildren": false,
      "countChildren": 0,
      "type": 1,
      "pathType": 2,
      "isFolder": false
    }
  ]
}
```

请求字段说明：
- `insight_conversation_id`
  - 当前会话 ID
  - 必填
- `nodes`
  - 当前整棵树的已选节点对象数组
  - 节点既可以是文件，也可以是文件夹
  - 如果传文件夹，后端会自动深度展开，找到可导入的文件节点
- `nodes[].id`
  - UNS 节点 ID
- `nodes[].name`
  - 节点名称
- `nodes[].alias`
  - 节点别名
- `nodes[].hasChildren`
  - 是否有子节点
- `nodes[].countChildren`
  - 子节点数量
- `nodes[].type`
  - 节点类型
- `nodes[].pathType`
  - 节点路径类型
- `nodes[].isFolder`
  - 是否文件夹节点
- `nodes[].unsNodePath`
  - 当前节点在树中的祖先节点 ID 数组
  - 字段名历史保留，不再表示路径字符串
  - 前端在提交全量选中状态时携带，后端据此推导父节点半选状态
  - 顺序固定为“从最远父级到最近父级”
  - 例如节点层级为 `A / B / C / 文件D`，则文件 `D` 的 `unsNodePath` 为 `["A", "B", "C"]`

响应 `data` 结构：

```json
{
  "imported": [
    {
      "datasource_id": 16,
      "datasource_type": "table",
      "datasource_name": "报警记录表",
      "knowledge_tag": "_baojingjilubiao_5d3feea65c1942bdbb7a",
      "uns_node_id": "2035975648114712576",
      "datasource_schema": "{...}",
      "datasource_config_json": "{...}"
    }
  ],
  "failed": [
    {
      "id": "2035976562024194048",
      "message": "未查询到 UNS 节点详情"
    }
  ],
  "removed_datasource_ids": [16],
  "selections": [
    {
      "id": 21,
      "insight_namespace_id": 7,
      "insight_conversation_id": 19,
      "uns_node_id": "101",
      "uns_node_name": "abi测试",
      "uns_node_path": [],
      "is_folder": true,
      "expanded_uns_node_ids": ["2035975648114712576", "2035976562024194048"],
      "selection_state": "selected",
      "derived": false,
      "created_at": "2026-04-13T10:20:00",
      "updated_at": "2026-04-13T10:20:00"
    },
    {
      "id": "partial:100",
      "insight_namespace_id": 7,
      "insight_conversation_id": 19,
      "uns_node_id": "100",
      "uns_node_name": "",
      "uns_node_path": [],
      "is_folder": true,
      "expanded_uns_node_ids": [],
      "selection_state": "partial",
      "derived": true,
      "created_at": "2026-04-13T10:20:00",
      "updated_at": "2026-04-13T10:20:00"
    }
  ]
}
```

响应字段说明：
- `imported`
  - 本次成功导入或复用并绑定到当前会话的数据源列表
- `failed`
  - 本次失败的节点 ID 列表及错误原因
- `removed_datasource_ids`
  - 本次同步后，从当前会话解绑的数据源 ID 列表
- `selections`
  - 当前会话最新的 UNS 选择回显列表
- `imported[].uns_node_id`
  - 该数据源对应的 UNS 唯一节点 ID
- `selections[].uns_node_path`
  - 当前节点的祖先节点 ID 数组
  - 顺序固定为“从最远父级到最近父级”
  - 例如节点层级为 `A / B / C / 文件D`，则文件 `D` 的 `uns_node_path` 为 `["A", "B", "C"]`
- `selections[].selection_state`
  - `selected` 表示真实选中节点
  - `partial` 表示由子节点推导出的半选父节点，仅用于前端回显
- `selections[].derived`
  - 是否后端推导出的展示节点

说明：
- 后端会使用当前用户 `UserContext` 中的 token 调第三方 UNS 接口
- 树节点详情接口为：
  - `GET {SUPOS_GATEWAY}/inter-api/supos/uns/instance?id={id}`
- 后端会使用当前用户 `UserContext` 中初始化好的 `LakeRDS` 数据库名
- 导入后的数据源类型仍然是 `table`
- 导入时表名取详情接口返回的 `data.alias`，并在 `datasource_config_json.table_name` 中保存为 `public.{alias}`
- 字段定义从详情接口返回的 `data.fields` 转换
- `datasource_schema.description` 取详情接口返回的 `data.description`
- `datasource_config_json` 保持 `table` 数据源结构，包含 `database_name`、`table_name`、`uns_alias`、`uns_path`、`uns_path_name`
- 对于已经导入过的同一个 UNS 节点，后端会优先复用共享数据源，而不是重复创建
- 对于本次目标集合中已存在的数据源，不会重复拉取详情接口，只会复用并同步会话绑定
- 如果本次提交为空集合，后端会清空当前会话的 UNS 选择和对应绑定
- 如果 `nodes` 非空但展开、详情查询或导入过程失败，接口返回失败并附带 `failed`
- UNS 文件夹展开相关限制支持通过环境变量动态配置：
  - `UNS_MAX_EXPANDED_FILES`
  - `UNS_MAX_EXPAND_DEPTH`
  - `UNS_TREE_PAGE_SIZE`
  - `UNS_DETAIL_WORKERS`
  - `UNS_IMPORT_MAX_CONCURRENT`

### 4.4 修改空间数据源描述

`PUT /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}/description`

用途：

- 修改空间数据源的描述信息
- 实际更新字段为 `insight_datasource.datasource_schema.description`
- 只修改描述，不会覆盖 `properties`、`required` 等其他 schema 信息

请求体：

```json
{
  "description": "2026年报警记录明细表，包含报警时间、位号、报警等级等字段"
}
```

请求字段说明：

- `description`
  - 数据源描述文本
  - 可为空字符串；传空时表示清空描述

响应 `data` 字段说明：

- 返回更新后的数据源对象
- 字段结构与“获取空间数据源列表”中的单条数据源一致
- 前端通常会直接用返回值更新当前列表项，无需再等下一次刷新

适用场景：

- 空间数据源列表中，点击“编辑描述”后保存

### 4.5 分页读取外部上传数据源文件内容

`GET /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}/content`

用途：

- 读取外部上传文件数据源的原始文件内容
- 支持分页展示，供前端“查看数据”弹窗使用
- 仅支持 `local_file` 类型的数据源，即通过上传 Excel / CSV 文件创建的数据源

查询参数：

- `page_no`
  - 页码，从 `1` 开始
  - 可选，默认 `1`
  - 兼容驼峰参数 `pageNo`
- `page_size`
  - 每页行数
  - 可选，默认 `50`
  - 后端会按 `DATASOURCE_FILE_CONTENT_MAX_PAGE_SIZE` 限制最大值，默认最大 `200`
  - 兼容驼峰参数 `pageSize`

请求示例：

```http
GET /api/insight/namespaces/10/datasources/82/content?page_no=1&page_size=50
```

响应 `data` 示例：

```json
{
  "datasource_id": 82,
  "datasource_name": "设备信息表 / Sheet",
  "file_name": "设备信息表.xlsx",
  "sheet_name": "Sheet",
  "columns": ["设备ID", "设备名称", "设备类型", "制造商", "安装日期", "位置", "额定功率(kW)", "状态"],
  "rows": [
    {
      "设备ID": "EQ001",
      "设备名称": "数控机床-A1",
      "设备类型": "加工设备",
      "制造商": "沈阳机床",
      "安装日期": "2022-03-15",
      "位置": "车间A-01",
      "额定功率(kW)": 45,
      "状态": "运行中"
    }
  ],
  "page_no": 1,
  "page_size": 50,
  "max_page_size": 200,
  "total_rows": 10,
  "total_pages": 1,
  "has_next": false,
  "has_prev": false
}
```

响应字段说明：

- `columns`
  - 原始文件列名列表
- `rows`
  - 当前页数据行，字段名与 `columns` 对应
- `total_rows`
  - 原始文件数据行总数，不包含表头
- `total_pages`
  - 总页数
- `has_next` / `has_prev`
  - 是否存在下一页 / 上一页

失败场景：

- 数据源不存在
- 数据源不是 `local_file`
- 原始上传文件不存在或已被移动
- 文件格式不支持或解析失败

### 4.6 删除空间数据源

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

### 4.7 绑定数据源到会话

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

### 4.8 从会话解绑数据源

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

- 旧版同步 SSE 分析链路
- 当前主链路建议使用 `POST /api/agent/tasks` 提交任务，再使用轮次 SSE 接口订阅输出

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

### 5.3 提交异步分析任务

`POST /api/agent/tasks`

用途：

- 聊天输入主链路的新接口
- 只负责创建会话轮次并提交后台分析任务
- 不直接返回 SSE 内容，前端拿到 `turn_id` 后再订阅该轮次的 SSE 输出

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

成功响应示例：

```json
{
  "success": true,
  "data": {
    "task_id": "5d7e8dc66a1642dc98d7f114f7f92a2a",
    "username": "admin",
    "namespace_id": 7,
    "conversation_id": 19,
    "turn_id": 39,
    "task_type": "new_analysis",
    "status": "queued",
    "request": {
      "namespace_id": 7,
      "conversation_id": 19,
      "turn_id": 39,
      "user_message": "分析2024年Q4季度的销售趋势"
    },
    "error_message": "",
    "created_at": "2026-05-11T14:04:35.465959",
    "started_at": null,
    "finished_at": null
  },
  "message": "分析任务已提交",
  "code": 200
}
```

说明：

- 同一用户默认最多同时存在 20 个未完成分析任务，可通过 `DATAINSIGHT_ANALYSIS_TASK_MAX_ACTIVE_PER_USER` 调整。
- 同一会话同一时间只允许一个 `queued` 或 `running` 任务。
- 如果 Redis 流式队列不可用，提交任务会失败，避免创建无法订阅输出的任务。

常见失败：

- `409`
  - 当前会话已有分析任务正在执行
- `429`
  - 当前用户正在分析中的任务达到上限

### 5.4 查询分析任务

`GET /api/agent/tasks/{task_id}`

用途：

- 查询异步分析任务当前状态。
- 前端 SSE 异常断开时，可用该接口确认任务是否仍在运行。

成功响应 `data` 字段与 `POST /api/agent/tasks` 返回的任务对象一致。

任务状态：

- `queued`
  - 已提交，等待后台线程执行
- `running`
  - 正在分析
- `success`
  - 分析完成
- `failed`
  - 分析失败
- `cancelled`
  - 用户主动停止

### 5.5 停止分析任务

`POST /api/agent/tasks/{task_id}/cancel`

用途：

- 用户主动终止一个仍在排队或执行中的分析任务。
- 适用于新分析任务和刷新分析任务。
- 后端会把任务状态更新为 `cancelled`，并向对应轮次 SSE 队列推送 `stage=cancelled` 事件。
- 对刷新分析任务，停止时会保留原有分析结果，避免污染历史产物。

路径参数：

- `task_id`
  - 后台分析任务 ID，即 `POST /api/agent/tasks` 或刷新任务接口返回的 `task_id`

请求体：

无。

成功响应示例：

```json
{
  "success": true,
  "data": {
    "task_id": "5d7e8dc66a1642dc98d7f114f7f92a2a",
    "username": "admin",
    "namespace_id": 7,
    "conversation_id": 19,
    "turn_id": 39,
    "task_type": "new_analysis",
    "status": "cancelled",
    "request": {
      "namespace_id": 7,
      "conversation_id": 19,
      "turn_id": 39,
      "user_message": "分析2024年Q4季度的销售趋势"
    },
    "error_message": "用户已停止本轮分析。",
    "created_at": "2026-05-11T14:04:35.465959",
    "started_at": "2026-05-11T14:04:36.102211",
    "finished_at": "2026-05-11T14:05:10.331872"
  },
  "message": "分析任务已停止",
  "code": 200
}
```

如果任务已经结束，接口仍返回成功，`message` 为 `分析任务已结束`，`data` 为当前任务状态。

常见失败：

- `404`
  - 分析任务不存在，或当前用户无权操作该任务

停止事件示例：

```json
{
  "type": "error",
  "task_id": "5d7e8dc66a1642dc98d7f114f7f92a2a",
  "turn_id": 39,
  "conversation_id": 19,
  "stage": "cancelled",
  "level": "warning",
  "message": "用户已停止本轮分析。"
}
```

### 5.6 查询当前会话运行中轮次

`GET /api/insight/conversations/{conversation_id}/running-turn`

用途：

- 前端切换到某个会话窗口时调用。
- 判断当前会话是否存在未结束的轮次。
- 如果存在，前端应禁用输入框，并订阅该轮次 SSE 输出。

存在运行中任务时响应示例：

```json
{
  "success": true,
  "data": {
    "task_id": "5d7e8dc66a1642dc98d7f114f7f92a2a",
    "username": "admin",
    "namespace_id": 7,
    "conversation_id": 19,
    "turn_id": 39,
    "task_type": "new_analysis",
    "status": "running",
    "request": {},
    "error_message": "",
    "created_at": "2026-05-11T14:04:35.465959",
    "started_at": "2026-05-11T14:04:35.700000",
    "finished_at": null
  },
  "message": "操作成功",
  "code": 200
}
```

不存在运行中任务时：

```json
{
  "success": true,
  "data": null,
  "message": "操作成功",
  "code": 200
}
```

### 5.7 订阅轮次流式输出

`GET /api/insight/conversations/{conversation_id}/turns/{turn_id}/stream`

用途：

- 订阅指定轮次的 SSE 输出。
- SSE 内容来自 Redis List 临时队列，不再依赖提交任务时的 HTTP 连接。
- 页面切换、重新进入会话时，只要该轮次任务仍未结束，前端可以重新连接该接口继续接收后续输出。

请求方式：

- `GET`
- `Accept: text/event-stream`
- 无请求体

返回格式：

- 与 `POST /api/agent/stream` 的 SSE 事件格式一致。
- 常见事件仍为：
  - `session`
  - `status`
  - `assistant`
  - `result`
  - `done`
  - `error`

说明：

- Redis List 是临时输出队列，事件被消费后会从队列中移除。
- 任务结束后，前端收到 `done` 或 `error` 应关闭当前 SSE 连接，并重新拉取会话历史。
- 如果队列暂时没有事件，后端会保持 SSE 连接并输出 heartbeat。

### 5.8 提交原轮次异步刷新分析任务

`POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/task`

用途：

- 在原轮次上重新执行分析。
- 不新增新轮次。
- 不直接返回 SSE 内容，前端拿到任务信息后继续订阅：
  - `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}/stream`

请求体：

```json
{}
```

成功响应 `data` 字段与 `POST /api/agent/tasks` 返回的任务对象一致，其中：

- `task_type`
  - 固定为 `rerun`
- `turn_id`
  - 仍为原轮次 ID

说明：

- 重跑前会清理该轮旧的 assistant 消息、执行记录和产物。
- 成功后结果回写到同一个 `turn_id`。
- 同样受用户任务并发上限和会话运行中任务限制。

### 5.9 原轮次同步流式刷新分析

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

- 这是旧版同步 SSE 刷新链路，当前主链路建议使用 `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/task`
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

`GET /api/insight/collects`

用途：

- 拉取当前用户全部收藏
- 收藏按“用户”维度组织，不再按空间过滤

### 7.2 创建收藏

`POST /api/insight/collects`

当前行为补充：

- 收藏成功后，后端会统计当前用户的有效收藏总数
- 然后调用平台统计上报接口：
  - `POST /os/inter-api/log-collect-system/track`
- 上报内容包含：
  - `username`
  - `collect_insight_result_count`
- 该上报属于后端内部副作用，前端无需额外传参

常见请求体：

#### 收藏整轮结果

```json
{
  "collect_type": "turn",
  "target_id": 39,
  "title": "分析2024年Q4季度的销售趋势",
  "summary_text": "Markdown 报告",
  "insight_namespace_id": 7,
  "insight_conversation_id": 19
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
  - 前端无需传
  - 后端会根据 `collect_type` 和目标对象自动补齐
  - 当前返回里常见会生成：
    - `conversation`：`conversation_id`
    - `turn`：`turn_id`、`charts`
    - `artifact`：`turn_id`、`artifact_type`、`chart_spec`

#### 收藏单独图表

```json
{
  "collect_type": "artifact",
  "target_id": 88,
  "title": "2024年Q4销售趋势",
  "summary_text": "",
  "insight_namespace_id": 7,
  "insight_conversation_id": 19,
  "insight_artifact_id": 88
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

当前行为补充：

- 取消收藏成功后，后端也会重新统计当前用户的有效收藏总数
- 然后调用平台统计上报接口：
  - `POST /os/inter-api/log-collect-system/track`
- 上报内容与创建收藏一致：
  - `username`
  - `collect_insight_result_count`

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
- `submitAgentTask`
- `getAnalysisTask`
- `getRunningTurn`
- `streamTurnEvents`
- `submitRerunTurnTask`
- `streamRerunTurn`
- `listNamespaces`
- `createNamespace`
- `renameNamespace`
- `deleteNamespace`
- `listConversations`
- `createConversation`
- `renameConversation`
- `deleteConversation`
- `getConversationPrompt`
- `updateConversationPrompt`
- `deleteConversationPrompt`
- `getConversationHistory`
- `getTurnDetail`
- `bindConversationDatasource`
- `unbindConversationDatasource`
- `listNamespaceDatasources`
- `uploadNamespaceDatasource`
- `getNamespaceDatasourceContent`
- `fetchUnsTreeNodes`
- `importNamespaceUnsDatasources`
- `deleteNamespaceDatasource`
- `exportTurnPdf`
- `listCollects`
- `createCollect`
- `removeCollect`
- `listInsightTasks`
- `createInsightTask`
- `getInsightTask`
- `updateInsightTask`
- `deleteInsightTask`
- `runInsightTask`
- `getInsightTaskRun`
- `listInsightTaskRuns`

---

## 10. 前端对接建议

### 10.1 页面初始化

1. 调 `GET /api/insight/namespaces`
2. 进入默认空间后调 `GET /api/insight/conversations?namespace_id=...`
3. 选中会话后调 `GET /api/insight/conversations/{conversation_id}/history`
4. 调 `GET /api/insight/conversations/{conversation_id}/running-turn` 判断是否有未完成轮次
5. 如果存在未完成轮次，前端暂停输入框并订阅 `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}/stream`
6. 同时调 `GET /api/insight/namespaces/{namespace_id}/datasources?insight_conversation_id=...`
7. 数据源面板展示会话提示词入口时，调 `GET /api/insight/conversations/{conversation_id}/prompt?namespace_id=...`

### 10.2 空间数据源面板

1. 主列表使用：
   - `GET /api/insight/namespaces/{namespace_id}/datasources?insight_conversation_id=...`
2. `checked=true`
   - 表示该数据源已绑定当前会话
3. 勾选时：
   - `POST /api/insight/conversation/datasource/`
4. 取消勾选时：
   - `DELETE /api/insight/conversation/datasource/`
5. 编辑数据源描述时：
   - `PUT /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}/description`
6. 查看外部上传文件原始数据时：
   - `GET /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}/content?page_no=1&page_size=50`
7. 上传文件时：
   - `POST /api/insight/namespaces/{namespace_id}/datasources/upload`
8. 加载 UNS 树时：
   - `POST /api/insight/namespaces/{namespace_id}/uns/tree`
9. 加载当前会话 UNS 已选节点回显时：
   - `GET /api/insight/namespaces/{namespace_id}/uns/selections?insight_conversation_id=...`
10. 勾选或取消勾选 UNS 节点时：
   - `POST /api/insight/namespaces/{namespace_id}/datasources/import-uns`
   - 请求体传 `insight_conversation_id + 当前全量已选 nodes`
11. 删除数据源时：
   - `DELETE /api/insight/namespaces/{namespace_id}/datasources/{datasource_id}`
12. 配置会话提示词时：
   - 查询：`GET /api/insight/conversations/{conversation_id}/prompt?namespace_id=...`
   - 保存：`PUT /api/insight/conversations/{conversation_id}/prompt`
   - 清空：`DELETE /api/insight/conversations/{conversation_id}/prompt`

### 10.3 聊天分析

1. 输入消息：
   - `POST /api/agent/tasks`
2. 订阅当前轮次输出：
   - `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}/stream`
3. SSE 结束后刷新历史结果：
   - `GET /api/insight/conversations/{conversation_id}/history`
4. 切换会话窗口时判断是否存在运行中轮次：
   - `GET /api/insight/conversations/{conversation_id}/running-turn`
5. 查看详情：
   - `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`
6. 删除会话：
   - `DELETE /api/insight/conversations/{conversation_id}`
7. 刷新分析：
   - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/rerun/task`
   - 然后订阅 `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}/stream`
8. 导出 PDF：
   - `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`

---

## 11. LLM 模型接口

### 11.1 获取 LLM 模型列表

`GET /api/llm/models`

用途：

- 供前端获取平台 LLM 网关可用模型列表。
- 后端内部调用 `{SUPOS_GATEWAY}/os/llm-gateway/v1/models`。
- 平台接口鉴权优先使用服务端配置的 `SUPOS_DATAINSIGHT-SERVER_APPKEY`，未配置时回退使用当前请求的 `Authorization`。

响应 `data` 为模型数组，后端会根据当前用户的模型选择补充 `selected` 字段：

```json
[
  {
    "id": "360GPT_S2_V9",
    "object": "model",
    "created": 1677649963,
    "owned_by": "360",
    "selected": true
  }
]
```

示例响应：

```json
{
  "success": true,
  "data": [
    {
      "id": "360GPT_S2_V9",
      "object": "model",
      "created": 1677649963,
      "owned_by": "360",
      "selected": true
    }
  ],
  "message": "操作成功",
  "code": 200
}
```

### 11.2 切换当前 Agent 模型

`PUT /api/llm/models/selected`

用途：

- 切换当前用户选择的 Agent 模型。
- 请求体中的 `model_id` 必须来自 `GET /api/llm/models` 返回的模型 `id`。
- 切换后模型选择写入数据库，并刷新当前用户认证上下文缓存。
- Agent 实例按模型运行配置复用，多个用户选择同一个模型时复用同一个 Agent。

请求体：

```json
{
  "model_id": "MiniMax-M2.7-highspeed"
}
```

响应 `data` 与 `GET /api/llm/models` 一致，返回带 `selected` 字段的模型数组。

---

## 12. 洞察任务

洞察任务把一次成功分析固化为可重复执行的本地分析逻辑。任务运行读取最新数据，但不再次调用大模型，也不会覆盖来源会话和轮次的分析结果。

所有对外接口均按当前登录用户隔离数据。前端使用公共 `task_id`、`run_id` 访问任务和运行记录，不应依赖数据库自增 `id`。

### 12.1 调度规则

公共请求中的 `schedule` 支持以下结构：

每天：

```json
{
  "frequency": "daily",
  "time": "09:00"
}
```

工作日：

```json
{
  "frequency": "weekdays",
  "time": "09:00"
}
```

每周：

```json
{
  "frequency": "weekly",
  "time": "09:00",
  "weekdays": [1, 3, 5]
}
```

每月：

```json
{
  "frequency": "monthly",
  "time": "09:00",
  "day_of_month": 31
}
```

字段约定：

- `frequency`
  - `daily`：每天。
  - `weekdays`：工作日，后端归一为星期一至星期五。
  - `weekly`：每周指定星期执行。
  - `monthly`：每月指定日期执行。
  - 后端保留 `manual` 频率能力，但当前“加入洞察任务”产品入口不提供该选项；所有任务均可通过“立即执行”手动运行。
- `time`
  - `HH:mm` 格式，24 小时制。
- `weekdays`
  - 仅 `weekly` 使用。
  - 取值 `1` 至 `7`，分别表示星期一至星期日。
  - 至少选择一天。
- `day_of_month`
  - 仅 `monthly` 使用。
  - 取值 `1` 至 `31`。
  - 当月不存在该日期时，在当月最后一天执行。
- 时区
  - 前端请求不提交时区。
  - 后端统一使用中国标准时间 `Asia/Shanghai` 计算调度时间。
  - 响应中的 `timezone` 及 `schedule.timezone` 是后端归一化结果，前端无需提供配置入口。

### 12.2 任务与运行状态

任务状态 `status`：

- `enabled`：任务已启用，定时计划生效。
- `paused`：任务已暂停，不再产生新的定时运行。

任务状态不是执行状态。前端不得把 `enabled` 显示为“运行中”。

运行状态 `latest_run.status` 或运行记录 `status`：

- `queued`：等待执行。
- `running`：正在执行。
- `success`：执行成功。
- `failed`：执行失败。
- `cancelled`：执行已取消。

只有 `queued`、`running` 属于非终态。前端仅在存在非终态运行时轮询，进入终态后停止轮询。

运行记录的 `trigger_type`：

- `initial`：创建任务时固化的来源会话分析结果，不会再次执行查询或进入 Runner。
- `manual`：用户点击“立即执行”产生的本地运行。
- `scheduled`：调度计划产生的本地运行。

### 12.3 获取洞察任务列表

`GET /api/insight/tasks`

用途：

- 获取当前用户的全部有效洞察任务。
- 任务按更新时间倒序返回。
- 列表项只携带最近运行摘要，不携带完整报告、图表和表格。

响应 `data` 示例：

```json
[
  {
    "id": 12,
    "task_id": "7fe691274c4d4f31b455129ce2785ddf",
    "namespace_id": 6,
    "source_conversation_id": 99,
    "source_turn_id": 342,
    "active_revision_id": 12,
    "name": "产品月度成本利润洞察",
    "description": "按月刷新各产品成本和利润情况",
    "status": "enabled",
    "schedule": {
      "frequency": "monthly",
      "time": "09:00",
      "day_of_month": 1,
      "timezone": "Asia/Shanghai"
    },
    "timezone": "Asia/Shanghai",
    "next_run_at": "2026-09-01T01:00:00Z",
    "last_run_at": "2026-08-01T01:00:08Z",
    "run_count": 3,
    "latest_run": {
      "run_id": "1ef9d3556c0048fc93bc821b4db08ebf",
      "trigger_type": "scheduled",
      "scheduled_for": "2026-08-01T01:00:00Z",
      "status": "success",
      "parameters": {},
      "error_message": "",
      "execution_seconds": 8120,
      "started_at": "2026-08-01T01:00:00Z",
      "finished_at": "2026-08-01T01:00:08Z"
    },
    "created_at": "2026-07-20T02:30:00Z",
    "updated_at": "2026-08-01T01:00:08Z"
  }
]
```

前端主要使用字段：

- `task_id`：公共任务 ID，后续接口路径参数。
- `name`、`description`：任务名称和描述。
- `status`：任务启用状态。
- `schedule`：结构化调度规则。
- `next_run_at`、`last_run_at`：UTC 时间字符串，前端按本地时区格式化。
- `run_count`：历史运行总数。
- `latest_run`：最近运行摘要，可能为 `null`。

`execution_seconds` 虽沿用历史字段名，当前值的单位为毫秒；前端展示秒数时需要除以 `1000`。

### 12.4 创建洞察任务

`POST /api/insight/tasks`

用途：

- 从一次成功且可本地重复执行的分析结果创建洞察任务。
- 创建时后端冻结执行代码、参数协议、数据源绑定、查询快照和逻辑签名为不可变任务版本。
- 创建时同步将来源分析的报告、图表、表格、参数、查询快照和耗时写为一条成功的“初次执行”记录；不会重新查询数据或调用模型。

请求体：

```json
{
  "source_execution_id": 1194,
  "name": "产品月度成本利润洞察",
  "description": "每月刷新各产品成本和利润情况",
  "schedule": {
    "frequency": "monthly",
    "time": "09:00",
    "day_of_month": 1
  }
}
```

请求字段说明：

- `source_execution_id`
  - 必填，来源分析执行记录的数据库 ID。
  - 来源执行必须属于当前用户可访问的洞察空间。
  - 来源执行必须为 `success` 且包含可复用的 `generated_code`。
  - 会话历史和轮次详情中的 `latest_execution.is_runnable=true` 可作为前端启用“加入任务”的直接判断条件。
- `name`
  - 必填，去除首尾空格后不能为空。
  - 最大保存 260 个字符。
- `description`
  - 可选。
- `schedule`
  - 必填，结构见 12.1。
  - 不提交时区。

成功响应：HTTP `201`，`data` 为 12.5 所述任务详情对象。新任务的 `run_count` 为 `1`，`latest_run.trigger_type` 为 `initial`，`runs` 中包含该初次执行摘要；完整结果仍通过 12.9 的运行详情接口按 `run_id` 获取。

常见失败：

- HTTP `400`：来源执行不存在、不可复用、任务名称为空或调度规则非法。
- HTTP `400`：当前用户无权基于该执行记录创建任务。

### 12.5 获取洞察任务详情

`GET /api/insight/tasks/{task_id}`

用途：

- 获取任务定义、当前不可变版本摘要以及最近 50 条运行摘要。
- 该接口不返回历史运行的完整报告；需要通过 12.9 按 `run_id` 获取。

响应 `data` 在列表项字段基础上增加：

```json
{
  "task_id": "7fe691274c4d4f31b455129ce2785ddf",
  "name": "产品月度成本利润洞察",
  "status": "enabled",
  "schedule": {
    "frequency": "monthly",
    "time": "09:00",
    "day_of_month": 1,
    "timezone": "Asia/Shanghai"
  },
  "run_count": 3,
  "latest_run": {},
  "revision": {
    "id": 12,
    "version": 1,
    "source_execution_id": 1194,
    "runtime_contract_version": "v1",
    "logic_signature": "sha256-signature",
    "created_at": "2026-07-20T02:30:00Z",
    "snapshot": {
      "source_execution_id": 1194,
      "analysis_question": "分析今年各产品的成本和利润",
      "title": "产品成本利润分析",
      "description": "",
      "used_datasource_ids": [179, 181],
      "datasource_bindings": [
        {
          "datasource_id": 179,
          "datasource_name": "产品月度利润_公司产线",
          "datasource_type": "postgresql"
        }
      ],
      "query_criteria": [
        {
          "query_key": "monthly_product_profit",
          "query_constraints": {
            "time_range": {
              "start": "2026-01-01",
              "end": "2026-12-31"
            },
            "target_filters": [],
            "aggregation": {
              "grain": "product_month",
              "metrics": ["cost", "profit"]
            }
          },
          "query_scope": {
            "datasource_ids": [179, 181]
          },
          "datasource_ids": [179, 181],
          "execution": {
            "status": "success",
            "row_count": 36
          }
        }
      ],
      "parameter_schema": {},
      "default_parameters": {},
      "logic_signature": "sha256-signature",
      "runtime_contract_version": "v1"
    }
  },
  "runs": [
    {
      "run_id": "1ef9d3556c0048fc93bc821b4db08ebf",
      "status": "success",
      "trigger_type": "manual",
      "scheduled_for": "2026-08-12T02:00:00Z",
      "execution_seconds": 8120
    }
  ]
}
```

统计口径展示建议使用：

- 任务定义：`revision.snapshot.query_criteria`。
- 当前选择运行的参数值：运行详情 `parameters`。
- 当前选择运行的真实查询快照：运行详情 `result_payload.query_snapshots`。

`time_range` 若是随任务触发时间滚动的相对窗口，`revision.snapshot.default_parameters` 使用以下声明形式：

```json
{
  "time_range": {
    "kind": "relative_time_range",
    "amount": 10,
    "unit": "minute",
    "anchor": "execution_time",
    "timezone": "Asia/Shanghai",
    "end_exclusive": true
  }
}
```

立即执行接口应原样提交该声明。后端会以本次 run 的 `scheduled_for` 为统一锚点物化绝对起止时间；执行完成后，运行记录 `parameters` 与 `result_payload.analysis_parameters` 均返回本次实际使用的绝对时间窗口。

前端不得展示任务版本中的生成代码；详情响应也不会返回该代码。

任务不存在或不属于当前用户时返回 HTTP `404`。

### 12.6 更新洞察任务

`PUT /api/insight/tasks/{task_id}`

请求体字段均为可选，仅更新显式提交的字段：

```json
{
  "name": "产品月度成本利润洞察",
  "description": "每月刷新",
  "schedule": {
    "frequency": "monthly",
    "time": "09:00",
    "day_of_month": 1
  },
  "status": "enabled"
}
```

字段说明：

- `name`：非空字符串。
- `description`：任务描述。
- `schedule`：完整调度规则，不提交时区。
- `status`：仅支持 `enabled`、`paused`。

更新调度规则后，后端重新计算 `next_run_at`。暂停任务时 `next_run_at` 变为 `null`；恢复任务时按原调度规则重新计算。

成功响应 `data` 为完整任务详情对象。

### 12.7 删除洞察任务

`DELETE /api/insight/tasks/{task_id}`

用途：

- 软删除任务。
- 同时暂停任务并清空下一次执行时间。
- 删除后不再出现在任务列表中。

成功响应：

```json
{
  "success": true,
  "data": null,
  "message": "洞察任务已删除",
  "code": 200
}
```

任务不存在或不属于当前用户时返回 HTTP `404`。

### 12.8 立即执行洞察任务

`POST /api/insight/tasks/{task_id}/runs`

用途：

- 使用当前请求用户的认证上下文立即执行一次本地分析。
- 手动执行不进入定时任务 Runner，也不使用 Service Account 身份。
- 用户 Token 仅在当前请求内传递给 Runtime，不写入任务定义或运行历史。

请求体：

```json
{
  "analysis_parameters": {}
}
```

字段说明：

- `analysis_parameters`
  - 可选对象。
  - 应符合 `revision.snapshot.parameter_schema`。
  - 没有可变参数时提交 `{}`，或省略后由后端使用任务版本的默认参数。

成功响应：HTTP `200`。响应中的运行记录已经进入终态。

```json
{
  "success": true,
  "data": {
    "run_id": "8ab8d07ee42c4df4a76787aa52069857",
    "task_definition_id": 12,
    "revision_id": 12,
    "trigger_type": "manual",
    "scheduled_for": "2026-08-12T02:00:00Z",
    "status": "success",
    "parameters": {},
    "error_message": "",
    "execution_seconds": 8120
  },
  "message": "任务运行已完成",
  "code": 200
}
```

若本地代码执行失败，接口仍返回该次运行记录，`data.status` 为 `failed`，具体原因见 `data.error_message`。前端应刷新任务详情并选中这条终态记录，不需要为手动执行启动轮询。

### 12.9 获取单次运行详情

`GET /api/insight/tasks/{task_id}/runs/{run_id}`

用途：

- 获取一次运行的完整结果。
- 任务报告、图表、表格只在该接口中完整返回。

成功响应 `data` 示例：

```json
{
  "run_id": "8ab8d07ee42c4df4a76787aa52069857",
  "task_definition_id": 12,
  "revision_id": 12,
  "trigger_type": "manual",
  "scheduled_for": "2026-08-12T02:00:00Z",
  "status": "success",
  "parameters": {},
  "error_message": "",
  "execution_seconds": 8120,
  "started_at": "2026-08-12T02:00:00Z",
  "finished_at": "2026-08-12T02:00:08Z",
  "analysis_report": "## 管理层摘要\n...",
  "result_payload": {
    "analysis_report": "## 管理层摘要\n...",
    "charts": [
      {
        "title": "各产品利润对比",
        "chart_spec": {}
      }
    ],
    "tables": [
      {
        "title": "产品成本利润明细",
        "columns": [
          {"field": "product_name", "title": "产品"},
          {"field": "profit", "title": "利润"}
        ],
        "rows": [
          {"product_name": "产品A", "profit": 128000}
        ]
      }
    ],
    "analysis_parameters_schema": {},
    "analysis_parameters": {},
    "query_snapshots": {}
  }
}
```

展示约定：

- 报告使用顶层 `analysis_report`。
- 图表使用 `result_payload.charts[].chart_spec`，复用洞察结果现有图表组件。
- 表格使用 `result_payload.tables[]`，复用洞察结果现有表格组件。
- 失败运行通过 `error_message` 展示失败原因。
- 当前本地任务重跑不重新生成 Evidence，前端不要假定任务运行结果包含可点击证据链。

任务或运行不存在、或不属于当前用户时返回 HTTP `404`。

### 12.10 分页获取执行记录

`GET /api/insight/tasks/{task_id}/runs?page_no=1&page_size=50`

查询参数：

- `page_no`
  - 从 `1` 开始，默认 `1`。
- `page_size`
  - 默认 `50`。
  - 最小 `1`，最大 `100`。

响应 `data`：

```json
{
  "items": [
    {
      "run_id": "8ab8d07ee42c4df4a76787aa52069857",
      "trigger_type": "manual",
      "scheduled_for": "2026-08-12T02:00:00Z",
      "status": "success",
      "parameters": {},
      "error_message": "",
      "execution_seconds": 8120,
      "started_at": "2026-08-12T02:00:00Z",
      "finished_at": "2026-08-12T02:00:08Z"
    }
  ],
  "total": 63,
  "page_no": 1,
  "page_size": 50,
  "has_more": true
}
```

列表中的运行记录不携带 `analysis_report` 和 `result_payload`。用户点击某条记录时，再调用 12.9 获取完整结果。

### 12.11 内部任务执行接口

`POST /api/insight/tasks/internal/runs/{run_id}/execute`

该接口只供洞察任务 Runner 通过 Gateway 调用，前端禁止调用。

约束：

- 请求必须由 `SUPOS_DATAINSIGHT-SERVER_APPKEY` 对应的服务账号发起。
- Gateway 将原始 appkey 放入 `origin-token`，将服务账号 JWT 放入 `Authorization`。
- 后端校验服务账号身份后执行已入队任务。
- 响应只包含运行状态和审计字段，不经 Gateway 返回完整报告和图表。

非服务账号请求返回 HTTP `403`。
