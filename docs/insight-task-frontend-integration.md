# 洞察任务前端对接指南

本文档面向需要接入“洞察任务”的其他前端项目。接口事实以当前后端实现为准，完整字段定义参见 [backend-api-spec.md](./backend-api-spec.md) 第 12 章。

## 1. 能力范围

洞察任务用于把一次成功分析固化为可重复执行的本地任务，并支持：

- 从分析结果创建任务。
- 每天、工作日、每周、每月定时执行。
- 所有任务均支持立即执行。
- 暂停、恢复和删除任务。
- 查看结构化统计口径。
- 查看最新或历史运行的分析报告、图表和表格。
- 分页查看执行记录。

当前范围不包括：

- “仅手动执行”频率选项。
- 前端时区选择。后端统一使用中国标准时间 `Asia/Shanghai`。
- 历史结果对比功能。
- 任务本地重跑结果的 Evidence 重新生成。
- 前端直接调用内部任务执行接口。

## 2. 页面入口与布局

### 2.1 导航入口

在“我的收藏”上方增加一级入口“洞察任务”。入口点击后打开洞察任务面板。

建议使用宽抽屉或独立页面，采用左右布局：

- 左侧：任务列表。
- 右侧：当前任务详情。

窄屏下可切换为上下布局。

### 2.2 任务卡片

每张任务卡片展示：

- 任务名称。
- 任务状态：`已启用` 或 `已暂停`。
- 调度频率。
- 历史运行次数。
- 最近一次运行状态。
- 下一次运行时间。

任务状态与运行状态必须分开：

- `task.status=enabled` 显示“已启用”，不能显示“运行中”。
- 是否正在执行由 `task.latest_run.status` 或该任务运行列表中的 `queued/running` 判断。

### 2.3 任务详情

任务详情使用同一页面连续展示，不使用 Tab：

1. 统计口径。
2. 分析报告。
3. 执行记录。

“统计口径”按照原型实现为页面内折叠区，默认收起：

```html
<details class="task-caliber-section">
  <summary>统计口径 <small>结构化 · 可复用</small></summary>
  <!-- 结构化口径内容 -->
</details>
```

不要在统计口径上方或下方额外展示以下内容块：

- 分析目标。
- 数据绑定。
- 执行计划。
- 时区。

任务频率已经在左侧任务卡片展示，不在统计口径区域重复展示。

分析报告和执行记录保持在统计口径下方。用户点击某条成功执行记录后，页面中的分析报告区域切换为该次运行结果。

不实现“历史结果对比”入口或对比状态。

## 3. 接口封装

所有接口使用当前登录用户认证信息。Axios 示例：

```js
export function listInsightTasks() {
  return api.get('/insight/tasks')
}

export function createInsightTask(payload) {
  return api.post('/insight/tasks', payload)
}

export function getInsightTask(taskId) {
  return api.get(`/insight/tasks/${taskId}`)
}

export function updateInsightTask(taskId, payload) {
  return api.put(`/insight/tasks/${taskId}`, payload)
}

export function deleteInsightTask(taskId) {
  return api.delete(`/insight/tasks/${taskId}`)
}

export function runInsightTask(taskId, analysisParameters = {}) {
  return api.post(`/insight/tasks/${taskId}/runs`, {
    analysis_parameters: analysisParameters || {}
  })
}

export function getInsightTaskRun(taskId, runId) {
  return api.get(`/insight/tasks/${taskId}/runs/${runId}`)
}

export function listInsightTaskRuns(taskId, pageNo = 1, pageSize = 50) {
  return api.get(`/insight/tasks/${taskId}/runs`, {
    params: { page_no: pageNo, page_size: pageSize }
  })
}
```

前端不得调用：

```text
POST /api/insight/tasks/internal/runs/{run_id}/execute
```

该接口只供任务 Runner 的服务账号使用。

## 4. 核心数据类型

以下 TypeScript 类型只保留前端需要依赖的字段，后端可能返回更多审计字段。

```ts
type InsightTaskStatus = 'enabled' | 'paused'
type InsightTaskRunStatus = 'queued' | 'running' | 'success' | 'failed' | 'cancelled'
type InsightTaskTriggerType = 'manual' | 'scheduled'

interface InsightTaskSchedule {
  frequency: 'daily' | 'weekdays' | 'weekly' | 'monthly'
  time: string
  weekdays?: number[]
  day_of_month?: number
}

interface InsightTaskRunSummary {
  run_id: string
  trigger_type: InsightTaskTriggerType
  scheduled_for: string
  status: InsightTaskRunStatus
  parameters: Record<string, unknown>
  error_message: string
  execution_seconds: number
  started_at?: string | null
  finished_at?: string | null
}

interface InsightTaskListItem {
  task_id: string
  namespace_id: number
  source_conversation_id: number
  source_turn_id: number
  name: string
  description: string
  status: InsightTaskStatus
  schedule: InsightTaskSchedule
  next_run_at?: string | null
  last_run_at?: string | null
  run_count: number
  latest_run?: InsightTaskRunSummary | null
}

interface InsightTaskRevisionSnapshot {
  source_execution_id: number
  analysis_question: string
  title: string
  description: string
  used_datasource_ids: number[]
  datasource_bindings: Array<{
    datasource_id: number
    datasource_name: string
    datasource_type: string
  }>
  query_criteria: QueryCriterion[]
  parameter_schema: Record<string, unknown>
  default_parameters: Record<string, unknown>
  logic_signature: string
  runtime_contract_version: string
}

interface QueryCriterion {
  query_key: string
  query_constraints: Record<string, any>
  query_scope: Record<string, any>
  datasource_ids: number[]
  execution: {
    status?: string
    row_count?: number
  }
}

interface InsightTaskDetail extends InsightTaskListItem {
  revision: {
    id: number
    version: number
    source_execution_id: number
    runtime_contract_version: string
    logic_signature: string
    snapshot: InsightTaskRevisionSnapshot
  } | null
  runs: InsightTaskRunSummary[]
}

interface InsightTaskRunDetail extends InsightTaskRunSummary {
  analysis_report: string
  result_payload: {
    analysis_report?: string
    charts?: Array<Record<string, any>>
    tables?: Array<Record<string, any>>
    analysis_parameters_schema?: Record<string, unknown>
    analysis_parameters?: Record<string, unknown>
    query_snapshots?: Record<string, any>
  }
}
```

注意：`execution_seconds` 当前存储单位为毫秒，展示秒数时使用：

```js
const seconds = (run.execution_seconds / 1000).toFixed(1)
```

## 5. 创建任务

### 5.1 “加入任务”按钮可用条件

按钮放在一次分析结果的收藏按钮旁边。

来源执行满足以下条件时启用：

```js
const canCreateTask = Boolean(
  latestExecution?.id
  && latestExecution?.execution_status === 'success'
  && latestExecution?.is_runnable === true
)
```

兼容尚未返回 `is_runnable` 的旧接口数据时，可以临时使用存在非空 `generated_code` 作为回退判断；新前端优先只依赖 `is_runnable`。

不可用时建议提供原因提示：

- 当前轮次没有可复用执行记录。
- 只有成功的分析结果可以加入任务。
- 当前执行记录缺少可复用分析逻辑。

### 5.2 创建弹窗

表单字段：

- 任务名称，必填。
- 任务描述，可选。
- 任务频率，必填。
- 执行时间，必填。
- 每周任务：执行星期，至少一天。
- 每月任务：执行日期，范围 1 至 31。

频率选项只提供：

- 每天：`daily`。
- 工作日：`weekdays`。
- 每周：`weekly`。
- 每月：`monthly`。

不提供：

- “仅手动执行”。
- 时区选择。

创建请求示例：

```js
const schedule = {
  frequency: form.frequency,
  time: form.time || '09:00'
}

if (form.frequency === 'weekly') {
  schedule.weekdays = [...form.weekdays]
}
if (form.frequency === 'monthly') {
  schedule.day_of_month = form.dayOfMonth || 1
}

await createInsightTask({
  source_execution_id: latestExecution.id,
  name: form.name.trim(),
  description: form.description.trim(),
  schedule
})
```

请求中不要添加 `timezone`。后端会统一补充 `Asia/Shanghai`。

创建成功后：

1. 关闭弹窗。
2. 提示“洞察任务已创建”。
3. 刷新任务列表。
4. 不改变来源会话和轮次的当前结果。

## 6. 加载任务详情

打开洞察任务面板时：

1. 调用 `GET /api/insight/tasks`。
2. 如果尚未选中任务，选择列表第一项。
3. 调用 `GET /api/insight/tasks/{task_id}` 获取任务详情和最近 50 条运行摘要。
4. 默认选择最近一条成功运行。
5. 调用 `GET /api/insight/tasks/{task_id}/runs/{run_id}` 获取完整报告、图表和表格。

任务详情和运行详情是两级请求，不能从任务列表项直接读取完整报告。

切换任务时，应清空上一任务的选中运行和报告，避免短暂显示其他任务的结果。

异步请求需要增加请求序号或取消机制，避免快速切换任务时旧请求覆盖新任务：

```js
let detailRequestId = 0

async function selectTask(taskId) {
  const requestId = ++detailRequestId
  const response = await getInsightTask(taskId)
  if (requestId !== detailRequestId) return
  selectedTask.value = response.data.data
}
```

## 7. 统计口径

统计口径不是任务名称、分析问题、数据源列表和执行计划的简单拼接。它应从任务版本与当前运行的结构化查询事实生成。

数据优先级：

1. 已选择运行存在 `result_payload.query_snapshots` 时，使用本次真实查询快照。
2. 否则使用 `task.revision.snapshot.query_criteria` 中的固化来源口径。
3. 参数值优先使用 `run.parameters`，没有运行时使用 `revision.snapshot.default_parameters`。

建议展示字段：

- 时间范围：`query_constraints.time_range`。
- 维度：`aggregation.group_by`、`dimensions`、`dimension` 或 `grain`。
- 指标：`aggregation.metrics`、`measures`、`metric` 或 `measure`。
- 过滤：`query_constraints.target_filters`。
- 参数取值：结合 `parameter_schema.parameters` 的标签和选项显示。
- 去重规则：`dedup_rule`、`distinct_by`、`aggregation.distinct_field` 等。
- 业务定义：`notes` 或 `business_definition`。
- 口径来源：`revision.snapshot.datasource_bindings[].datasource_name`。

相同值需要去重；空值使用稳定的用户可读兜底文案，不展示内部 JSON、查询键、生成代码或逻辑签名。

折叠区默认关闭，用户点击标题后展开。不要在外面重复展示“分析目标”“数据绑定”“执行计划”或“时区”卡片。

## 8. 立即执行与轮询

### 8.1 提交运行

所有任务均可立即执行，与任务是否配置定时频率无关。

```js
const response = await runInsightTask(
  task.task_id,
  task.revision?.snapshot?.default_parameters || {}
)
```

成功响应为 HTTP `202`，返回的运行状态通常为 `queued`。前端应立即把该运行插入当前任务的运行列表，并显示“等待执行”。

同一任务已经存在 `queued` 或 `running` 运行时，禁用“立即执行”，避免用户重复提交。这个判断必须限定到当前任务，不能因为其他任务正在运行而禁用全部任务。

### 8.2 轮询策略

只有在以下条件同时成立时轮询：

- 洞察任务面板仍然打开。
- 至少一个当前可见任务存在 `queued` 或 `running` 运行。

建议间隔为 2 秒：

```js
function schedulePolling() {
  clearPolling()
  if (!panelVisible.value || !hasPendingRun()) return
  timer = window.setTimeout(pollTasks, 2000)
}
```

每次轮询：

1. 静默刷新任务列表，更新最近运行状态。
2. 刷新当前任务详情，更新运行摘要列表。
3. 选中运行从非终态进入终态时，调用运行详情接口获取完整结果。
4. 所有运行进入终态后不再创建下一次定时器。

禁止永久 `setInterval` 轮询。面板关闭和组件卸载时必须清理定时器。

### 8.3 状态文案

```js
const runStatusLabels = {
  queued: '等待执行',
  running: '正在执行',
  success: '执行成功',
  failed: '执行失败',
  cancelled: '执行已取消'
}
```

运行失败时展示 `error_message`。历史成功记录仍保留在执行记录中，用户可以重新选择并查看对应报告。

## 9. 报告、图表和表格

完整结果来自运行详情接口：

- Markdown 报告：`run.analysis_report`。
- 图表：`run.result_payload.charts`。
- 表格：`run.result_payload.tables`。

渲染组件应复用现有洞察分析结果组件，避免任务页形成第二套产物解释标准。

图表容器在隐藏状态下挂载可能得到错误尺寸。使用抽屉时应在抽屉 `opened` 后再挂载图表，或者在可见后调用图表实例的 `resize()`。

表格列优先使用 `columns[].title`，字段取值使用 `columns[].field`：

```html
<th v-for="column in table.columns" :key="column.field">
  {{ column.title || column.field }}
</th>
```

当前任务本地重跑不重新生成 Evidence。任务页面可以正常展示报告、图表和表格，但不要把不存在的 Evidence 引用渲染为可点击状态。

## 10. 执行记录

任务详情接口只返回最近 50 条执行摘要。超过 50 条时使用分页接口继续加载：

```text
GET /api/insight/tasks/{task_id}/runs?page_no=2&page_size=50
```

列表按 `scheduled_for` 倒序展示：

- 触发方式：手动执行或定时执行。
- 运行状态。
- 计划执行时间。
- 执行耗时。

点击任意记录调用运行详情接口，并更新上方分析报告区域。不要新增单独的报告 Tab，也不要实现历史结果对比区域。

分页合并时按 `run_id` 去重，避免轮询刷新与“加载更多”同时发生后出现重复记录。

## 11. 暂停、恢复与删除

暂停：

```js
await updateInsightTask(task.task_id, { status: 'paused' })
```

恢复：

```js
await updateInsightTask(task.task_id, { status: 'enabled' })
```

暂停只影响后续定时调度，不等同于取消已经入队或正在执行的运行。

删除前需要二次确认：

```js
await deleteInsightTask(task.task_id)
```

删除成功后：

1. 清空当前任务和当前运行。
2. 刷新任务列表。
3. 如果仍有任务，自动选择第一项。

## 12. 错误与空状态

至少覆盖以下状态：

- 任务列表加载中。
- 尚无洞察任务。
- 任务详情加载中。
- 尚无执行记录。
- 尚未选择成功执行记录。
- 运行等待中和运行中。
- 运行失败及错误信息。
- 任务不存在或已删除。
- 来源执行不可复用，创建按钮禁用。

接口错误提示优先使用：

```js
error?.response?.data?.message || error?.message || fallbackMessage
```

任务列表或详情的一次刷新失败，不应清空用户当前已经看到的成功报告。

## 13. 对接验收清单

- “洞察任务”入口位于“我的收藏”上方。
- 成功且 `is_runnable=true` 的分析结果可点击“加入任务”。
- 创建弹窗只有任务名称、描述和调度频率相关字段。
- 创建弹窗没有“仅手动执行”和时区配置。
- 每月任务支持 1 至 31 日。
- 请求中不提交 `timezone`，任务仍按中国标准时间调度。
- 任务卡片分别展示任务启用状态与最近运行状态。
- 任务详情不使用 Tab。
- 统计口径为默认收起的页面内折叠区。
- 页面不展示额外的分析目标、数据绑定、执行计划和时区卡片。
- 页面不包含历史结果对比功能。
- 点击执行记录可以切换上方报告、图表和表格。
- 立即执行返回 `queued` 后能自动刷新为最终状态。
- 只有存在 `queued/running` 时轮询，终态或关闭面板后停止。
- 两个任务之间的运行状态、详情请求和轮询不会互相覆盖。
- 超过 50 条执行记录可以继续分页加载，且不会重复。
- 图表在可见容器中完成初始化，不出现空白或尺寸错误。
- 失败记录展示 `error_message`，历史成功记录仍可选择查看。
