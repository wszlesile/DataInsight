# 证据链前端对接文档

本文档说明分析结果证据链功能的前端接入方式，面向需要复用该能力的前端项目。证据链能力的目标是：用户点击分析结果中的图表数值、表格指标单元格或报告中的关键数值后，能够打开该数值的计算证据链，查看数据来源、筛选范围、数据处理、计算公式、可信说明、原始 SQL 和执行代码。

## 1. 总体链路

证据链不是通过 `raw_value` 反查数值，而是通过后端随分析产物返回的 `evidence_refs` 定位。

整体流程：

1. 前端加载会话历史中的分析结果数据。
2. 后端在图表、表格、报告产物中返回 `evidence_refs` 或 `report_evidence_refs`。
3. 前端渲染产物时读取 refs，判断哪些图表字段、表格列、报告文本范围可以点击。
4. 用户点击可追溯位置后，前端携带 `artifact_id + evidence_id + selection` 调用证据链详情接口。
5. 后端校验当前产物是否真的关联该 `evidence_id`，然后返回链路步骤、SQL 和执行代码。
6. 前端以抽屉、弹窗或侧栏展示证据链详情。

## 2. 涉及接口

### 2.1 获取会话历史

```http
GET /insight/conversations/{conversation_id}/history
```

当前页面主列表使用该接口获取每轮分析结果。与证据链相关的字段：

```json
{
  "turn_id": 72,
  "report": "Markdown 报告正文",
  "report_evidence_refs": {
    "report_trace_refs": []
  },
  "charts": [
    {
      "id": 210,
      "title": "图表标题",
      "chart_spec": {},
      "evidence_refs": {
        "chart_trace_refs": []
      }
    }
  ],
  "tables": [
    {
      "id": 214,
      "columns": [],
      "rows": [],
      "evidence_refs": {
        "table_trace_refs": []
      }
    }
  ]
}
```

说明：

- `charts[*].id`、`tables[*].id` 是后续查询证据链详情所需的 `artifact_id`。
- 报告的 `artifact_id` 在历史列表中不一定直接作为顶层字段暴露，但 `report_evidence_refs.report_trace_refs[*].artifact_id` 中会带有。
- 如果某个产物没有 `evidence_refs`，则该产物没有可点击证据链。

### 2.2 解析证据链详情

```http
POST /insight/conversations/{conversation_id}/turns/{turn_id}/evidence/resolve
Content-Type: application/json
```

请求体：

```json
{
  "artifact_id": 210,
  "evidence_id": "ev_476_165698fa0179",
  "detail_level": "full",
  "selection": {
    "type": "chart",
    "chart_index": 0,
    "series_index": 0,
    "data_index": 2,
    "value_field": "物料成本_元每件",
    "raw_value": 52.41
  }
}
```

字段说明：

- `artifact_id`：被点击的产物 ID，必须传。
- `evidence_id`：从 ref 中读取，必须传。
- `detail_level`：可传 `summary` 或 `full`。当前前端使用 `full`。
- `selection`：前端点击上下文，用于后端在详情里展示“当前点击值”和定位信息。

不同产物的 `selection` 建议：

图表：

```json
{
  "type": "chart",
  "chart_index": 0,
  "series_index": 0,
  "data_index": 2,
  "value_field": "毛利_元",
  "raw_value": 10378407.57
}
```

表格：

```json
{
  "type": "table",
  "table_index": 0,
  "row_index": 1,
  "column_key": "毛利(元)",
  "raw_value": 10378407.57
}
```

报告：

```json
{
  "type": "report",
  "start": 456,
  "end": 471,
  "raw_value": "10,378,407.57 元"
}
```

响应体核心结构：

```json
{
  "traceability_status": "available",
  "metric_name": "毛利合计",
  "selected_context": {
    "type": "chart",
    "display_value": "10,378,407.57 元",
    "raw_value": 10378407.57
  },
  "chain_steps": [
    {
      "type": "read_data",
      "title": "读取数据",
      "summary": "从产品月度利润表读取数据。",
      "items": [
        {"label": "数据源", "value": "产品月度利润_公司产线"}
      ],
      "output": "查询输出 3 行聚合结果。",
      "details": {}
    },
    {
      "type": "filter_scope",
      "title": "筛选范围",
      "summary": "限定 2026 年 3 月数据。",
      "items": [],
      "output": "查询条件校验通过。",
      "details": {}
    },
    {
      "type": "data_filter",
      "title": "数据过滤",
      "summary": "查询后未记录额外过滤处理。",
      "items": [],
      "output": "",
      "details": {}
    },
    {
      "type": "calculation",
      "title": "数据计算",
      "summary": "按产品汇总毛利。",
      "method": "求和",
      "formula": "SUM(毛利_元)",
      "result_value": "10,378,407.57 元",
      "items": [],
      "details": {}
    },
    {
      "type": "reliability",
      "title": "结果校验与可信说明",
      "summary": "证据链生成于已完成的分析执行。",
      "items": [],
      "badges": [],
      "warnings": [],
      "details": {}
    }
  ],
  "execution_ref": {
    "conversation_id": 48,
    "turn_id": 72,
    "execution_id": 476,
    "analysis_task_id": null
  },
  "debug_sql": "SELECT ...",
  "generated_code": "execution_intent = 'analysis'\n..."
}
```

异常状态也会以成功响应的 data 返回，前端可根据 `traceability_status` 展示空态：

- `available`：可展示证据链。
- `invalid_request`：请求缺少 `artifact_id` 或 `evidence_id`。
- `unavailable`：当前产物没有可用证据链。
- `not_found`：未找到对应 Evidence，或该产物位置未关联该 Evidence。

## 3. evidence_refs 结构

`evidence_refs` 写在产物元数据中：

```json
{
  "chart_trace_refs": [],
  "table_trace_refs": [],
  "report_trace_refs": []
}
```

### 3.1 图表 refs

```json
{
  "chart_index": 0,
  "value_field": "毛利_元",
  "ref_id": "ref_0214c1a61e",
  "evidence_id": "ev_476_796c2f0b0947",
  "artifact_id": 212,
  "artifact_type": "chart"
}
```

定位规则：

- `chart_index` 是当前轮次图表列表中的 0 基下标。
- `value_field` 对应图表数据字段，通常来自 `chart_document.encoding.value_field` 或 `value_fields`。
- 前端图表点击事件中，根据 ECharts 事件的 `encode.x`、`encode.y`、`seriesName` 或 `seriesIndex` 找到匹配 ref。
- 找到 ref 后，使用 `ref.artifact_id` 和 `ref.evidence_id` 调用详情接口。

当前前端参考逻辑：

```js
const findChartEvidenceRef = (chart, chartIndex, params = {}) => {
  const refs = chart?.evidenceRefs?.chart_trace_refs || []
  const encode = params.encode || {}
  const valueFields = [
    ...(Array.isArray(encode.y) ? encode.y : []),
    ...(Array.isArray(encode.x) ? encode.x : []),
    params.seriesName
  ].map((item) => String(item || '').trim()).filter(Boolean)

  return refs.find((ref) =>
    Number(ref.chart_index || 0) === Number(chartIndex || 0)
    && (!ref.value_field || valueFields.includes(ref.value_field))
  )
}
```

### 3.2 表格 refs

```json
{
  "table_index": 0,
  "column_key": "毛利(元)",
  "row_scope": "all",
  "ref_id": "ref_170a4bd449",
  "evidence_id": "ev_476_796c2f0b0947",
  "artifact_id": 214,
  "artifact_type": "table"
}
```

定位规则：

- 表格 refs 当前按指标列绑定，而不是按每个单元格单独绑定。
- `column_key` 或 `value_column` 对应表格列的 `field/key/title/label`。
- `row_scope=all` 表示该列所有行共享同一条计算逻辑。
- 前端渲染表格时，先判断当前列是否有 ref；有 ref 才绑定点击事件或鼠标样式。

当前前端参考逻辑：

```js
const findTableEvidenceRef = (table, column) => {
  const refs = table?.evidence_refs?.table_trace_refs || []
  return refs.find((ref) =>
    ref.column_key === column.field || ref.column_key === column.title
  )
}
```

### 3.3 报告 refs

```json
{
  "start": 456,
  "end": 471,
  "text": "10,378,407.57 元",
  "ref_id": "ref_05e9301281",
  "evidence_id": "ev_476_d67d5f2c2fbc",
  "artifact_id": 213,
  "artifact_type": "report"
}
```

定位规则：

- `start/end` 是最终 Markdown 源文本中的字符区间。
- `text` 必须等于 `analysis_report.slice(start, end)`。
- 前端渲染 Markdown 前，先在 Markdown 源文本阶段把命中的字符范围包成轻量 HTML，如 `<span class="traceable-value">...</span>`，再交给 Markdown 渲染器。
- 点击 span 后，从 `dataset` 中读取 `evidence_id/start/end`，再从 refs 中找到完整 ref。

当前前端参考逻辑：

```js
const renderTraceableReport = (content, refs = []) => {
  const text = String(content || '')
  const normalizedRefs = refs
    .filter((ref) => ref && Number.isFinite(Number(ref.start)) && Number.isFinite(Number(ref.end)) && ref.evidence_id)
    .map((ref) => ({ ...ref, start: Number(ref.start), end: Number(ref.end) }))
    .sort((left, right) => left.start - right.start)

  let cursor = 0
  let markdown = ''
  normalizedRefs.forEach((ref) => {
    if (ref.start < cursor) return
    markdown += text.slice(cursor, ref.start)
    const targetText = text.slice(ref.start, ref.end)
    markdown += `<span class="traceable-value" data-evidence-id="${ref.evidence_id}" data-start="${ref.start}" data-end="${ref.end}">${targetText}</span>`
    cursor = ref.end
  })
  markdown += text.slice(cursor)
  return marked.parse(markdown)
}
```

注意：报告定位必须在 Markdown 源文本阶段做，不建议在 HTML DOM 渲染后再按文本搜索，否则同值、多处重复值和 Markdown 转换会导致定位不稳定。

## 4. 前端接入步骤

### 4.1 归一化产物数据

图表：

```js
const normalizeChartItem = (chart) => ({
  id: chart.id || 0,
  title: chart.title || '',
  chartSpec: chart.chart_spec || null,
  evidenceRefs: chart.evidence_refs || chart.metadata_json?.evidence_refs || {}
})
```

表格：

```js
const normalizeTables = (tables = []) => (
  tables.map((table) => ({
    ...table,
    evidence_refs: table.evidence_refs || table.metadata_json?.evidence_refs || {}
  }))
)
```

报告：

```js
const normalizeReportRefs = (value) => {
  if (Array.isArray(value)) return value
  return value?.report_trace_refs || []
}
```

### 4.2 渲染时判断可点击位置

- 图表：一般不需要明显视觉标记；点击点位时查找 ref，找不到则忽略。
- 表格：可以对有 ref 的指标列添加 `cursor: pointer` 或轻量 hover 样式。
- 报告：将命中范围包成 `.traceable-value`，可添加下划线、虚线边框或 hover 样式。

### 4.3 点击后调用详情接口

```js
async function resolveEvidenceDetail(conversationId, turnId, ref, selection) {
  const response = await resolveEvidence(conversationId, turnId, {
    artifact_id: ref.artifact_id,
    evidence_id: ref.evidence_id,
    selection,
    detail_level: 'full'
  })
  return response.data?.data
}
```

### 4.4 展示证据链详情

建议展示结构：

- 顶部：`metric_name`、当前点击值、状态。
- 主体：按 `chain_steps` 顺序展示。
- 附加抽屉或折叠面板：
  - 查看 SQL：展示 `debug_sql`。
  - 查看执行代码：展示 `generated_code`。

当前后端步骤类型固定为：

1. `read_data`：读取数据。
2. `filter_scope`：筛选范围。
3. `data_filter`：数据过滤。
4. `calculation`：数据计算。
5. `reliability`：结果校验与可信说明。

前端不要依赖中文标题判断步骤，应优先使用 `type`。

### 4.5 `/evidence/resolve` 响应字段展示规则

证据链详情接口返回的 `chain_steps` 已经是面向前端展示加工后的链路结构。前端主视图应优先消费 `title`、`summary`、`items`、`method`、`formula`、`output`、`result_value`、`badges`、`warnings`，不要直接把 `details` 当成主内容展示。`details` 主要用于调试、排障或后续扩展。

#### 4.5.1 顶部信息

抽屉或弹窗顶部建议这样取值：

| 展示位置 | 取值字段 | 展示说明 |
| --- | --- | --- |
| 主标题 | `selected_context.display_value`，为空时用 `selected_context.raw_value` | 展示“当前点击的实际值”，例如 `10,378,407.57 元 是怎么计算出来的？`。 |
| 副标题 | `metric_name` | 展示当前值对应的指标名称，例如 `毛利合计`、`销售收入`、`毛利率`。 |
| 空态提示 | `traceability_status`、`message` | 当 `traceability_status !== "available"` 时，不展示链路步骤，直接展示 `message` 或通用空态。 |
| 定位上下文 | `selected_context` | 可选展示为小标签，例如图表第几个点、表格第几行、报告文本范围。通常不作为主内容。 |

当前前端主标题逻辑可参考：

```js
const getEvidenceDisplayValue = (detail) => {
  const context = detail?.selected_context || {}
  return context.display_value ?? context.raw_value ?? '当前数值'
}
```

#### 4.5.2 链路步骤通用渲染

每个 `chain_steps[*]` 都可以按同一套基础模板渲染：

| 字段 | 是否主展示 | 展示方式 |
| --- | --- | --- |
| `type` | 是 | 稳定步骤类型，用于判断步骤语义和样式，不建议直接展示给用户。 |
| `title` | 是 | 步骤标题，作为链路节点标题展示。 |
| `summary` | 是 | 步骤核心说明，作为该步骤的第一段说明文字。 |
| `items` | 是 | 键值列表，展示该步骤的结构化信息。每项使用 `label` 作为名称，`value` 作为内容。 |
| `method` | 是，仅计算步骤常见 | 展示计算方法、算法口径或生成方式。 |
| `formula` | 是，仅计算步骤常见 | 展示公式，建议用等宽或公式样式突出。 |
| `output` | 是 | 展示该步骤产出的结果说明，例如查询命中行数、处理完成说明。 |
| `result_value` | 是，仅计算步骤常见 | 展示当前点击位置对应的最终值。 |
| `badges` | 是 | 展示状态标签，例如“查询口径已校验”。 |
| `warnings` | 是 | 展示风险、限制、缺失、抽样等提示。 |
| `details` | 否 | 调试字段，不建议直接渲染到主链路。 |

当前前端的主渲染逻辑就是按这个模型处理：`title/summary/items/method/formula/output/badges/warnings/result_value` 进入链路卡片，`debug_sql/generated_code` 进入底部折叠面板。

#### 4.5.3 每个链路步骤取什么字段

`read_data`：读取数据

| 展示内容 | 取值字段 | 说明 |
| --- | --- | --- |
| 步骤标题 | `step.title` | 固定展示为“读取数据”。 |
| 数据来源摘要 | `step.summary` | 说明从什么数据源读取数据。 |
| 数据源、类型、字段 | `step.items` | 展示数据源名称、数据源类型、本指标依赖字段等。 |
| 查询输出 | `step.output` | 展示查询产出了什么数据，例如命中多少行、得到多少条聚合结果。 |
| 调试信息 | `step.details.query_refs`、`step.details.source_refs` | 不建议主展示；如需要排查，可放在开发调试区。 |

`filter_scope`：筛选范围

| 展示内容 | 取值字段 | 说明 |
| --- | --- | --- |
| 步骤标题 | `step.title` | 固定展示为“筛选范围”。 |
| 筛选口径摘要 | `step.summary` | 用一句话说明本次计算限定了什么范围。 |
| 过滤规则 | `step.items` | 展示字段、比较方式和值，例如“创建时间：2026-03-01 至 2026-03-31”“产品编码：等于 P-STD-001”。 |
| 执行/校验结果 | `step.output` | 展示查询口径是否通过校验、命中记录数等。 |
| 原始口径 | `step.details.query_constraints` | 不建议主展示；后端已将其转换为 `summary/items/output`。 |

`data_filter`：数据过滤

| 展示内容 | 取值字段 | 说明 |
| --- | --- | --- |
| 步骤标题 | `step.title` | 固定展示为“数据过滤”。 |
| 代码层处理摘要 | `step.summary` | 说明查询后是否经过 Python 代码进一步清洗、转换、排除或过滤。 |
| 处理动作 | `step.items` | 展示具体处理动作，例如排除空值、转换金额单位、过滤软删除数据。没有处理动作时可为空。 |
| 处理输出 | `step.output` | 展示处理后的状态，例如“查询命中记录直接进入计算”。 |
| 原始处理记录 | `step.details.transformations` | 不建议主展示；如果要排查生成质量，可放调试区。 |

`calculation`：数据计算

| 展示内容 | 取值字段 | 说明 |
| --- | --- | --- |
| 步骤标题 | `step.title` | 固定展示为“数据计算”。 |
| 算法摘要 | `step.summary` | 说明当前值如何从筛选后的数据计算得到。 |
| 计算方法 | `step.method` | 展示聚合、比例、排名、算法说明等，例如“对毛利_元执行求和计算”。 |
| 计算公式 | `step.formula` | 展示公式，例如 `SUM(毛利_元)`、`毛利率 = 毛利 / 销售收入 * 100%`。 |
| 当前值 | `step.result_value`，为空时用 `selected_context.display_value/raw_value` | 展示用户点击位置对应的实际结果值。 |
| 中间指标 | `step.items` | 展示分子、分母、基础指标、分组维度、指标字段等结构化信息。 |
| 原始推导 | `step.details.derivation`、`step.details.calculation`、`step.details.intermediate_outputs` | 不建议直接主展示；后端已抽取为 `summary/method/formula/items`。 |

`reliability`：结果校验与可信说明

| 展示内容 | 取值字段 | 说明 |
| --- | --- | --- |
| 步骤标题 | `step.title` | 固定展示为“结果校验与可信说明”。 |
| 可信摘要 | `step.summary` | 说明证据链是否记录了口径、来源、计算逻辑，以及是否有质量说明。 |
| 校验项 | `step.items` | 展示查询口径校验、命中记录数、质量说明等。 |
| 状态标签 | `step.badges` | 展示“查询口径已校验”“证据链已生成”等标签。 |
| 风险提示 | `step.warnings` | 展示抽样、缺失、限制、截断等质量说明。 |
| 原始执行信息 | `step.details.execution`、`step.details.quality_notes` | 不建议主展示；必要时用于排查。 |

#### 4.5.4 SQL 和执行代码

`/evidence/resolve` 顶层会返回两个调试字段：

| 展示入口 | 取值字段 | 展示方式 |
| --- | --- | --- |
| 查看 SQL | `debug_sql` | 作为纯文本展示原生 SQL；不要再解析成 query_snapshots 结构给用户看。 |
| 查看执行代码 | `generated_code` | 作为纯文本展示本轮分析执行的 Python 代码。 |

这两个字段建议放在证据链主链路下方的折叠面板中，不和 5 个链路步骤混在一起。当前前端已经按“查看 SQL”“查看执行代码”两个折叠项处理。

## 5. 安全与校验

后端会做以下校验：

- `artifact_id` 必须属于当前 `conversation_id + turn_id`。
- `evidence_id` 必须存在于当前执行的 `evidence_map`。
- 当前产物的 `metadata_json.evidence_refs` 必须包含该 `evidence_id`，防止跨产物伪造请求。

前端仍建议：

- 不要自行拼接不存在的 `evidence_id`。
- 不要用 `raw_value` 查找证据链。
- 对 `traceability_status !== "available"` 做空态展示。
- 对 `debug_sql/generated_code` 做纯文本展示，不作为 HTML 渲染。

## 6. 常见问题

### 为什么不用 raw_value 定位？

同一个报告、表格或图表中可能出现多个相同数值。仅按真实值反查会出现一值多义或错误命中。当前方案使用产物位置 ref 绑定证据链，避免同值冲突。

### 图表和表格为什么是字段级 ref？

图表点和表格单元格可能很多，但同一指标字段通常共享同一套计算逻辑。后端按字段级 ref 记录计算逻辑，前端点击具体点位时把 `data_index/row_index/raw_value` 作为 `selection` 传给详情接口，用于展示当前点击上下文。

### 报告为什么要 start/end？

报告是 Markdown 自然语言，不像图表和表格有稳定字段。`start/end` 是最稳定的定位方式；前端应在 Markdown 源文本阶段包裹对应文本。

### 如果没有 evidence_refs 怎么办？

说明该产物没有可追溯信息，前端按普通分析结果渲染即可，不展示可点击证据链交互。

## 7. 接入检查清单

- 已从历史、详情或版本接口读取 `evidence_refs`。
- 图表点击事件能拿到 `seriesIndex/dataIndex/encode/value`。
- 表格列模型能统一出 `field/title`。
- 报告渲染前能拿到 Markdown 源文本和 `report_trace_refs`。
- 点击时传入 `artifact_id`、`evidence_id` 和合理的 `selection`。
- 详情接口返回 `available` 时展示 `chain_steps`。
- SQL 和执行代码以纯文本展示。
- 无证据链或非 available 状态有空态兜底。
