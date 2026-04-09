# DataInsight 上下文工程设计

本文档描述当前 DataInsight 项目中，多轮数据洞察 Agent 的上下文管理设计。重点说明：

- 为什么会话是主上下文边界
- 一轮分析的历史事实如何保存
- 下一轮对话时，历史上下文如何重新组装给模型
- 数据源、执行记录、图表和报告分别承担什么职责

## 1. 设计目标

当前上下文工程要同时满足四件事：

1. 支持会话内连续追问和持续分析
2. 支持分析结果回放、重跑和溯源
3. 避免把完整历史无脑塞进 Prompt 导致上下文膨胀
4. 在不改变主架构的前提下，为后续扩展更多会话和资源类型预留空间

## 2. 上下文边界

### 2.1 空间边界

`namespace_id` 表示资源归属边界。

空间负责承载：

- 空间级数据源
- 空间级默认资源关系
- 空间下的会话集合

### 2.2 会话边界

`conversation_id` 是真正的上下文主边界。

会话负责承载：

- 当前分析主线
- 活动数据源快照
- 滚动摘要
- 当前分析状态
- 多轮历史问答与执行轨迹

### 2.3 轮次边界

`turn` 是历史事实边界。

每一轮保存的是“这一轮当时真实发生了什么”，包括：

- 用户问题
- 本轮使用的数据源范围
- 本轮最终回答
- 本轮执行记录
- 本轮派生产物

## 3. 关键数据模型职责

### 3.1 会话与轮次

#### `insight_ns_conversation`

会话主表，保存当前状态和会话级快照。

当前与上下文最相关的字段：

- `title`
- `status`
- `summary_text`
- `active_datasource_snapshot`
- `last_turn_no`
- `last_message_at`

其中：

- `summary_text` 保存滚动摘要文本
- `active_datasource_snapshot` 保存当前会话最新的数据源快照

#### `insight_ns_turn`

轮次表，保存单轮分析事实。

关键字段：

- `conversation_id`
- `turn_no`
- `user_query`
- `selected_datasource_ids_json`
- `selected_datasource_snapshot_json`
- `final_answer`
- `status`
- `error_message`

这两个数据源字段的职责分别是：

- `selected_datasource_ids_json`
  - 本轮实际使用的数据源 ID 列表
- `selected_datasource_snapshot_json`
  - 本轮分析开始时冻结的数据源快照

### 3.2 消息

#### `insight_ns_message`

消息表负责保存后续上下文回放真正需要的用户/助手消息。

当前主要保存：

- 用户问题
- assistant 最终回答
- assistant 错误消息

它不承担完整工具执行日志存储职责。

### 3.3 执行记录

#### `insight_ns_execution`

执行表是“代码生成与执行型分析”场景里最关键的表之一。

它保存：

- 生成的 Python 代码
- 执行状态
- 错误信息
- 标准输出和错误输出
- 结构化执行结果 `result_payload_json`
- 分析报告 `analysis_report`

它承担的是“分析主逻辑留痕”，而不是纯展示结果。

### 3.4 派生产物

#### `insight_ns_artifact`

产物表负责保存面向用户展示和后续引用的分析产物。

当前主要产物类型：

- `chart`
- `report`
- `table`

典型内容：

- `chart`
  - 保存在 `content_json.chart_spec`
- `report`
  - 保存在 `content_json.report_markdown`
- `table`
  - 保存在 `content_json.columns / rows`

### 3.5 会话记忆

#### `insight_ns_memory`

记忆表负责保存会话级压缩记忆，而不是逐条消息历史。

当前有两类记忆：

- `rolling_summary`
- `analysis_state`

其中：

- `rolling_summary`
  - 用于压缩自然语言历史
- `analysis_state`
  - 用于保存结构化分析状态

## 4. 数据源上下文设计

### 4.1 数据源归属

当前数据源是空间级资源：

- 上传文件数据源 -> 保存到 `insight_datasource`
- 归属某个 `namespace`

会话是否使用某个数据源，通过 `insight_ns_rel_datasource` 决定。

### 4.2 为什么既要会话级快照，又要轮次级快照

当前设计同时保留：

- `conversation.active_datasource_snapshot`
- `turn.selected_datasource_snapshot_json`

原因是这两个状态语义不同：

#### 会话级快照

表示：

- 当前会话“最新”的数据源使用范围

用途：

- 下一轮默认续用
- 当前状态展示
- Prompt 组装时构建数据源上下文

#### 轮次级快照

表示：

- 本轮分析开始时“真实使用”的数据源范围

用途：

- 历史回放
- 结果溯源
- 原轮重跑

如果只保留会话级快照，后面切换数据源后，历史轮次就会失真。

## 5. 一轮分析的运行链路

### 5.1 启动新一轮分析

入口在：

- `ConversationContextService.start_run()`

主要步骤：

1. 查找或创建会话
2. 确保会话级资源绑定存在
3. 计算下一轮 `turn_no`
4. 刷新会话级数据源快照
5. 冻结本轮数据源快照
6. 创建 `insight_ns_turn`
7. 写入用户消息到 `insight_ns_message`

### 5.2 重跑原轮次

入口在：

- `ConversationContextService.start_rerun()`

语义是：

- 在同一个 `turn_id` 上重新执行分析
- 不新增新轮次

重跑前会清理该轮旧的：

- assistant 消息
- execution
- artifact

然后复用：

- 原问题
- 原数据源快照

## 6. Prompt 组装设计

Prompt 组装入口在：

- `src/agent/__init__.py`
- `src/agent/context_engineering.py`

当前发送给模型的上下文顺序是：

1. 系统提示词
2. 数据源上下文
3. 会话记忆上下文
4. 历史问答消息
5. 当前用户问题

当前已经针对 MiniMax 做了一个额外约束：

- 所有系统级上下文会先合并成一条 `SystemMessage`
- 历史对话仍按 `HumanMessage / AIMessage` 传入

这样可以避免因为多条 `SystemMessage` 导致模型侧参数校验失败。

## 7. 会话记忆设计

### 7.1 `rolling_summary`

滚动摘要由最近若干轮构建而成，用于在长会话里保留主要历史脉络。

当前策略是：

- 优先保留真实成功执行过的轮次结论
- 避免把仅靠自然语言复述、没有真实执行支撑的结果继续污染后续上下文

### 7.2 `analysis_state`

结构化分析状态通常包含：

- `active_datasource_snapshot`
- `recent_turn_datasource_usage`
- `recent_execution_summaries`
- `latest_execution`
- `latest_artifacts`
- `last_turn_no`

这部分不是给用户看的，而是给下一轮分析继续读取的状态摘要。

## 8. 执行与产物如何参与下一轮上下文

### 8.1 执行记录的作用

最近执行摘要会被写入记忆，帮助模型理解：

- 最近分析做了什么
- 最近代码执行状态如何
- 最近失败在哪里

当前不会把完整 `result_payload_json` 直接回灌给模型，以避免图表配置和表格结果把上下文撑爆。

### 8.2 派生产物的作用

最近产物也会参与会话记忆，但只保留轻量摘要：

- 产物类型
- 标题
- 摘要
- 图表系列数量 / 表格行列数等概览信息

不会把完整 `chart_spec` 直接塞回会话上下文。

## 9. 历史回放与前端展示

### 9.1 会话历史

前端主聊天区通过：

- `GET /api/insight/conversations/{conversation_id}/history`

拿到按轮次聚合后的历史结果。

每个轮次结果卡主要由以下内容组成：

- 用户问题
- 报告
- 图表列表
- 表格列表
- 最近执行摘要
- 执行次数

### 9.2 单轮详情

前端详情抽屉通过：

- `GET /api/insight/conversations/{conversation_id}/turns/{turn_id}`

读取完整轮次信息，包括：

- messages
- executions
- latest_execution
- artifacts

### 9.3 PDF 导出

PDF 导出走：

- `POST /api/insight/conversations/{conversation_id}/turns/{turn_id}/export/pdf`

后端会从该轮产物中读取：

- chart artifact
- report artifact

动态生成 PDF，不依赖会话记忆。

## 10. 当前设计能解决什么问题

这套设计当前解决的是：

- 多轮分析不是纯聊天历史堆叠，而是“会话状态 + 轮次事实 + 执行记录 + 派生产物”的组合
- 历史轮次的数据源范围不会被后续轮次覆盖
- 原轮重跑不会新增新轮次，便于用户在同一结果卡上刷新分析
- 前端历史展示、收藏、导出都能基于同一套轮次产物结构工作

## 11. 一句话总结

当前 DataInsight 的上下文工程，以 `conversation` 作为持续分析的主边界，以 `turn` 作为历史事实边界，用 `message + memory + execution + artifact` 共同支撑多轮数据洞察。

其中：

- `execution` 负责保存真正的分析主逻辑
- `artifact` 负责保存面向展示和复用的结果产物
- `memory` 负责控制后续 Prompt 长度并保留分析主线

这样既能支持连续分析，也能保证结果可回放、可重跑、可溯源。
