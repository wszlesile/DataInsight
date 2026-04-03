# DataInsight Agent 上下文管理设计

## 1. 文档目标

本文档用于说明 DataInsight 项目中数据洞察 Agent 的上下文管理设计，重点回答以下问题：

- 系统如何支持多轮、持续的即席数据洞察对话
- 对话记录、执行记录和派生产物如何持久化保存
- 下一轮对话时，历史上下文如何重新组织并注入大模型
- 数据源、会话、轮次、执行记录之间的职责边界如何划分

## 2. 设计原则

### 2.1 上下文边界

当前系统采用以下边界定义：

- `namespace_id`：资源归属边界
- `conversation_id`：上下文边界
- `turn`：单轮分析事实边界

这意味着：

- 空间负责承载资源归属
- 会话负责承载多轮上下文
- 轮次负责记录每一轮分析的历史事实

### 2.2 产物边界

本项目不是普通聊天机器人，而是“代码生成与执行型”数据分析 Agent。  
因此产物边界定义如下：

- 主产物：本轮由大模型生成并执行的 Python 代码
- 派生产物：代码执行后生成的图表、报告和结果文件

这也是上下文管理与普通聊天场景最大的区别。下一轮追问时，真正重要的不只是“上轮说了什么”，还包括：

- 上轮选了哪些数据源
- 上轮生成了什么 Python 代码
- 上轮执行结果是什么

## 3. 业务边界

### 3.1 空间与会话

当前业务阶段中，一个洞察空间下通常只有一个活跃会话，实际使用关系近似为：

- `namespace : conversation = 1 : 1`

但系统建模已经按未来可扩展到：

- `namespace : conversation = 1 : N`

因此当前实现中，会话仍然被设计为独立实体，作为真正的上下文边界。

### 3.2 数据源与知识资源

当前业务约束如下：

- 知识资源是全局资源，可以被任意空间引用
- 数据源是空间隔离资源，只归属于某个空间

因此当前建模方式为：

- 全局知识资源实体
- 空间隔离的数据源实体
- 会话级资源绑定关系

## 4. 核心数据模型

### 4.1 会话相关

#### `insight_ns_conversation`

用于表示一条持续分析会话。关键字段包括：

- `id`
- `username`
- `insight_namespace_id`
- `title`
- `status`
- `summary_text`
- `active_datasource_snapshot`
- `last_turn_no`

其中：

- `summary_text` 保存会话级滚动摘要
- `active_datasource_snapshot` 保存当前会话最新的数据源选择快照

#### `insight_ns_turn`

用于表示会话中的一轮完整分析。关键字段包括：

- `conversation_id`
- `turn_no`
- `user_query`
- `selected_datasource_ids_json`
- `selected_datasource_snapshot_json`
- `final_answer`
- `status`

其中：

- `selected_datasource_ids_json` 保存本轮选中的数据源 ID 列表
- `selected_datasource_snapshot_json` 保存本轮分析开始时的数据源快照

#### `insight_ns_message`

用于保存真正参与上下文组装的消息明细。典型消息包括：

- 用户问题
- assistant 最终回答
- 错误消息

### 4.2 执行与产物相关

#### `insight_ns_execution`

这是上下文工程中的关键实体。  
它保存每一轮分析中大模型生成并执行的 Python 代码，以及执行结果。

关键字段包括：

- `conversation_id`
- `turn_id`
- `tool_call_id`
- `title`
- `description`
- `generated_code`
- `execution_status`
- `result_file_id`
- `analysis_report`
- `stdout_text`
- `stderr_text`
- `execution_seconds`
- `error_message`

它的职责是：

- 保存分析主逻辑
- 支撑后续多轮分析延续
- 支撑执行回放、问题排查和结果溯源

#### `insight_ns_artifact`

用于保存代码执行后生成的派生产物，例如：

- 图表
- 报告
- 结果文件

它不是分析主产物，而是用户展示层和历史回看的补充结果。

### 4.3 记忆相关

#### `insight_ns_memory`

用于保存压缩后的会话级记忆。当前主要包含两类：

- `rolling_summary`
- `analysis_state`

其中：

- `rolling_summary` 用于压缩自然语言历史
- `analysis_state` 用于保存结构化分析状态

### 4.4 数据源相关

#### `insight_datasource`

用于定义空间内的数据源实体。当前 `datasource_type` 已收敛为：

- `local_file`
- `minio_file`
- `table`
- `api`

同时，`knowledge_tag` 已经收敛为数据源主表字段，用于承载数据源级稳定标识，便于后续做向量索引、召回过滤和结果归属。

#### `insight_ns_rel_datasource`

用于定义“当前会话可引用哪些空间内数据源”的关系，不承载历史轮次事实，也不再保存数据源级标识信息。

## 5. 为什么数据源状态要分层保存

用户在每一轮对话前都可能重新选择数据源范围。  
因此系统必须同时保存两类状态：

### 5.1 会话级当前状态

保存位置：

- `insight_ns_conversation.active_datasource_snapshot`

含义：

- 当前会话最新激活的数据源范围

用途：

- 作为下一轮默认延续的数据源范围
- 作为当前会话状态的快速入口

### 5.2 轮次级历史事实

保存位置：

- `insight_ns_turn.selected_datasource_ids_json`
- `insight_ns_turn.selected_datasource_snapshot_json`

含义：

- 本轮分析开始时，实际选中了哪些数据源

用途：

- 保证历史轮次可追溯
- 避免后续轮次覆盖历史事实
- 支持“继续上一轮那个范围”这类追问

## 6. 多轮即席数据洞察的运行链路

### 6.1 请求进入

前端通过以下接口发起请求：

- `POST /api/agent/invoke`
- `POST /api/agent/stream`

请求核心字段包括：

- `namespace_id`
- `conversation_id`
- `user_message`

规则如下：

- `conversation_id` 为空时，表示新建会话
- 当前轮实际使用的数据源范围，不由 Agent 分析接口直接传入，而是由后端根据 `conversation_id` 查询会话级绑定关系得到

### 6.2 创建或恢复会话

入口：

- `src/agent/invoker.py`
- `src/service/conversation_context_service.py`

`ConversationContextService.start_run()` 的主要流程：

1. 根据 `conversation_id` 查询会话
2. 若不存在则创建新会话
3. 确保当前会话已经具备资源绑定关系
4. 计算下一轮 `turn_no`
5. 合并当前会话数据源快照
6. 冻结本轮数据源快照
7. 创建一条新的 `insight_ns_turn`
8. 写入一条用户消息到 `insight_ns_message`

### 6.3 处理本轮数据源范围

`start_run()` 内部会调用 `_merge_datasource_snapshot()`：

1. 读取会话级 `active_datasource_snapshot`
2. 如果本轮传入了 `datasource_ids`，先按会话绑定关系校验这些数据源是否可用
3. 校验通过后更新会话级最新快照
4. 同时生成本轮 `selected_datasource_snapshot_json`

结果是：

- 会话级记录“当前最新选择”
- 轮次级记录“本轮历史事实”

### 6.4 调用 Agent 分析

在会话和轮次落库后，`invoker` 会调用：

- `get_input()`
- `insight_agent.invoke()` 或 `insight_agent.stream()`

送入模型的上下文主要包括：

- 系统提示词
- 运行时系统配置
- 数据源上下文
- 历史问答消息
- 会话记忆
- 最近代码执行记录
- 最近派生产物摘要

### 6.5 执行 Python 代码

大模型按照 `sys_prompt.md` 的约束生成 Python 分析代码，并通过 `execute_python` 执行。

`execute_python` 的关键行为如下：

1. 在执行前创建一条 `insight_ns_execution`
2. 保存 `generated_code`、`title`、`description`
3. 执行代码
4. 成功时回写：
   - `execution_status`
   - `result_file_id`
   - `analysis_report`
   - `stdout_text`
   - `stderr_text`
   - `execution_seconds`
5. 失败时回写：
   - `execution_status = failed`
   - `error_message`
   - `stdout_text`
   - `stderr_text`

当前为了兼容现有 Agent 契约，`execute_python` 对外仍返回：

- `StructuredResult(file_id, analysis_report)`

也就是说：

- 外部工具契约不变
- 内部增加了完整执行留痕

### 6.6 分析完成后的持久化

分析成功时会调用 `complete_run()`：

1. 更新 `turn.final_answer`
2. 写入 assistant 最终回答消息
3. 查询本轮最近一次 `execution`
4. 生成派生产物记录
5. 刷新 `rolling_summary`
6. 刷新 `analysis_state`

分析失败时会调用 `fail_run()`：

1. 标记轮次失败
2. 写入错误消息
3. 刷新摘要与记忆

## 7. 对话记录是怎么保存的

系统不是只保存聊天文本，而是分层持久化：

- 会话层：会话元信息、当前快照、滚动摘要
- 轮次层：本轮问题、最终回答、本轮数据源范围
- 消息层：用户消息、assistant 回答、错误消息
- 执行层：生成代码、执行状态、执行输出、执行结果
- 产物层：图表、报告、结果文件
- 记忆层：摘要和分析状态

这种分层方式的好处是：

- 页面展示时可以按“会话 -> 轮次 -> 消息/执行/产物”组织数据
- 组装下一轮上下文时只提取必要内容
- 历史轮次的数据源范围和执行代码都可追溯
- 后续做收藏、审计、回放都更稳定

## 8. 下一轮对话时，历史上下文如何组装

上下文组装入口位于：

- `src/agent/context_engineering.py`

当前会构造四类上下文：

- 数据源上下文
- 历史问答上下文
- 记忆上下文
- 执行与产物上下文

### 8.1 数据源上下文

`get_datasource_message(namespace_id, conversation_id)` 的逻辑是：

1. 读取会话级 `active_datasource_snapshot`
2. 按 `conversation_id` 查询当前会话绑定的数据源
3. 关联到真实数据源定义
4. 组装成统一的运行时 JSON

当前注入模型的数据源上下文格式：

```json
{
  "datasources": [
    {
      "datasource_id": 1,
      "datasource_type": "table",
      "datasource_name": "sales_detail",
      "datasource_identifier": "sales_detail",
      "metadata_schema": {
        "name": "sales_detail",
        "description": "销售明细表",
        "properties": {},
        "required": []
      }
    }
  ],
  "selected_datasource_ids": [1]
}
```

含义：

- `datasources` 是当前会话可用于洞察的完整数据源集合
- `selected_datasource_ids` 是当前会话最近一次选中的分析范围

### 8.2 历史问答上下文

`get_history_messages(conversation_id)` 会提取最近的：

- 用户问题
- assistant 最终回答

默认只取最近 10 条，并转换为模型可消费的：

- `HumanMessage`
- `AIMessage`

### 8.3 记忆上下文

`get_memory_messages(conversation_id)` 会补充两类系统消息：

#### `rolling_summary`

保存最近若干轮对话的压缩摘要，用于降低 prompt 长度，同时保留主线。

#### `analysis_state`

保存当前分析状态，主要包括：

- `active_datasource_snapshot`
- `recent_turn_datasource_usage`
- `recent_execution_summaries`
- `latest_execution`
- `latest_artifacts`
- `last_turn_no`

它不是简单聊天历史，而是“当前分析已经推进到哪里”的结构化状态。

### 8.4 执行上下文

这是当前方案相对普通对话系统最关键的升级点。

`get_memory_messages(conversation_id)` 还会额外注入：

- 最近几次代码执行记录摘要
- 最近一次执行的 Python 分析代码

这样用户说：

- “继续用刚才的分析逻辑”
- “把上一轮代码改成按区域聚合”
- “还是用刚才那段逻辑，只把时间过滤改成 Q4”

模型都能直接参考最近代码和执行结果，而不是只依赖图表或报告去猜测。

### 8.5 派生产物上下文

最近图表和报告也会以系统消息形式补充到上下文中，但它们的定位是：

- 展示层产物
- 历史追溯资源
- 对执行结果的补充摘要

而不是主分析产物。

## 9. 页面如何回放历史会话

页面查询展示走标准 Web 查询链路，而不走 Agent 运行链路。

主要服务：

- `src/service/insight_ns_conversation_service.py`

其中：

- `get_conversation_history()`：返回整个会话的轮次时间线
- `get_turn_detail()`：返回单轮完整详情

### 9.1 会话历史

会话历史按 `turn_no` 顺序返回，每轮包括：

- 问题
- 本轮选中的数据源 ID
- 本轮数据源快照
- 报告内容
- 图表文件 ID
- 最新执行摘要
- 执行次数
- 状态
- 开始和结束时间

### 9.2 轮次详情

轮次详情会进一步返回：

- 本轮消息列表
- 本轮执行记录列表
- 本轮最新执行记录
- 本轮派生产物列表

## 10. 为什么这套方案能支持多轮即席数据洞察

因为系统同时保存了四类关键事实：

### 10.1 历史问答事实

通过 `insight_ns_message` 保存真实用户问题和助手回答。

### 10.2 历史分析状态

通过 `insight_ns_memory` 保存摘要和分析状态，避免每轮都依赖全量原始历史。

### 10.3 历史数据源事实

通过 `insight_ns_turn.selected_datasource_ids_json` 和 `selected_datasource_snapshot_json` 保存每轮分析时的数据源范围，确保：

- 当前轮切换数据源不会污染历史轮次
- 回看历史时知道当时基于哪些数据源得出结论
- 后续追问“继续上一轮那个范围”时有据可依

### 10.4 历史执行事实

通过 `insight_ns_execution` 保存每轮真正执行过的 Python 代码及其结果，确保：

- 后续问题可以延续上一轮分析逻辑
- 图表和报告都能追溯到对应执行
- 调试时能定位“结论是怎么计算出来的”

## 11. 一句话总结

当前上下文管理方案的核心思路是：

以 `conversation` 作为上下文主边界，以 `turn` 作为历史事实单元，以 `message + memory + execution + artifact` 共同组织多轮分析上下文；其中 `execution` 承载真正的分析主逻辑，`artifact` 承载用户可见的派生产物，再通过“会话级当前快照 + 轮次级历史快照”同时兼顾连续分析体验、历史可追溯性和未来从 `1:1` 平滑升级到 `1:N` 的演进能力。
