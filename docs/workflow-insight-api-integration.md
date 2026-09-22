# 业务协同工作流洞察分析接口对接说明

## 1. 接口用途

业务协同工作流可以直接提交数据洞察分析任务，无需提前创建洞察空间或会话。

调用方提供分析问题、允许分析的 UNS 节点范围、输出格式和可选分析偏好。洞察服务异步执行分析，调用方通过作业 ID 查询状态和最终结果。

本调用模式具有以下边界：

- 不发起需要用户继续作答的同轮次澄清；关键语义无法确定时，作业以失败状态结束并返回原因。
- 不生成 Evidence、Evidence ref 或证据链展示数据。
- 不会在调用方指定的 UNS 节点范围外扩展数据源。

## 2. 通用约定

### 2.1 接口地址

下文使用应用内相对路径：

```text
/api/insight/workflow/jobs
```

通过平台 Gateway 调用时，请在该路径前增加当前环境的数据洞察应用访问前缀。例如：

```text
http://{SUPOS_GATEWAY}/os/toolkit/data-insight-agent/api/insight/workflow/jobs
```

实际应用前缀以部署环境为准。

### 2.2 认证

所有接口均需携带当前调用用户的认证信息：

```http
Authorization: Bearer {token}
```

洞察服务会按该用户权限校验每一个 UNS 节点。无权限、节点不存在或节点无法转换为可分析数据源时，任务不会进入正式分析。

### 2.3 响应外壳

接口统一返回以下结构：

```json
{
  "success": true,
  "data": {},
  "message": "",
  "code": 200
}
```

HTTP 状态码和响应中的 `code` 保持一致。

### 2.4 作业状态

| 状态 | 含义 |
| --- | --- |
| `queued` | 已提交，等待执行 |
| `running` | 正在分析 |
| `success` | 执行成功，可读取 `result` |
| `failed` | 执行失败，可读取 `error` |
| `cancelled` | 已取消 |

调用方应轮询查询接口，直到进入 `success`、`failed` 或 `cancelled` 终态。建议轮询间隔为 2 至 5 秒。

## 3. 提交洞察作业

### 3.1 请求

```http
POST /api/insight/workflow/jobs
Content-Type: application/json
Authorization: Bearer {token}
Idempotency-Key: {optional-idempotency-key}
X-Request-ID: {optional-request-id}
```

请求体：

```json
{
  "analysis_question": "分析本周期质量风险，并说明主要异常和处理建议",
  "uns_node_ids": [
    "2066811253765967872",
    "2066811253765967873"
  ],
  "output_format": {
    "type": "standard"
  },
  "analysis_preferences": "重点关注 OOS 批次、过程波动和可执行建议"
}
```

### 3.2 参数说明

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `analysis_question` | 是 | string | 完整分析问题。上游变量应在提交前完成插值 |
| `uns_node_ids` | 是 | array[string] | 本次允许分析的 UNS 节点 ID。不是 alias、数据源 ID 或节点名称 |
| `output_format` | 是 | object | 输出契约，支持 `standard`、`text`、`json` |
| `analysis_preferences` | 否 | string | 补充业务规则、统计偏好和表达偏好；语义等同于洞察会话提示词 |

默认最多允许提交 100 个 UNS 节点；具体上限可由部署环境配置。

### 3.3 输出格式

#### 标准洞察结果

```json
{
  "type": "standard"
}
```

成功后返回 Markdown 分析报告、图表和表格。

#### 纯文本结果

```json
{
  "type": "text"
}
```

成功后只返回自然语言洞察文本，不生成图表和表格。

#### 严格 JSON 结果

```json
{
  "type": "json",
  "schema": {
    "type": "object",
    "properties": {
      "oos_batch_count": {
        "type": "integer",
        "description": "本周期 OOS 去重生产批次数"
      },
      "risk_level": {
        "type": "string",
        "enum": ["high", "medium", "low"],
        "description": "综合风险等级"
      },
      "summary": {
        "type": "string",
        "description": "基于分析结果形成的业务结论"
      }
    },
    "required": ["oos_batch_count", "risk_level", "summary"],
    "additionalProperties": false
  }
}
```

JSON Schema 根节点必须是 `object`。字段名、类型、`description`、枚举和必填项共同定义调用方需要的结果。洞察服务会在提交时校验 Schema 自身是否一致，并在执行完成后对最终对象做确定性 Schema 校验；Schema 无效或结果不满足 Schema 时均不会以成功状态返回。

首版支持以下安全子集：

- 类型：`object`、`array`、`string`、`number`、`integer`、`boolean`、`null`
- 对象：`properties`、`required`、`additionalProperties`
- 数组：`items`、`minItems`、`maxItems`
- 字符串：`minLength`、`maxLength`、`pattern`
- 数值：`minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum`

边界约束必须可满足。例如 `minimum` 不能大于 `maximum`，`minItems` 不能大于 `maxItems`，`minLength` 不能大于 `maxLength`；相等的上下界中包含排他边界也会形成无效空区间。这类请求直接返回 HTTP `400`，错误消息包含字段路径和冲突边界。
- 通用：`enum`、`const`、`title`、`description`

不支持 `$ref`、远程引用、递归引用及 `oneOf`、`anyOf`、`allOf` 等组合关键字。

### 3.4 幂等提交

建议调用方为一次业务工作流执行生成稳定的 `Idempotency-Key`：

- 同一用户、相同幂等键、相同请求内容：返回已有作业，不重复分析，HTTP `200`。
- 同一用户、相同幂等键、不同请求内容：返回 HTTP `400`。
- 首次创建作业：返回 HTTP `202`。

### 3.5 成功响应

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "queued",
    "analysis_question": "分析本周期质量风险，并说明主要异常和处理建议",
    "uns_node_ids": [
      "2066811253765967872",
      "2066811253765967873"
    ],
    "output_format": {
      "type": "standard"
    },
    "result": null,
    "error": "",
    "created_at": "2026-09-01T10:00:00",
    "started_at": null,
    "finished_at": null
  },
  "message": "洞察作业已提交",
  "code": 202
}
```

调用方只需保存 `job_id`，后续使用该 ID 查询或取消作业。

## 4. 查询洞察作业

### 4.1 请求

```http
GET /api/insight/workflow/jobs/{job_id}
Authorization: Bearer {token}
```

作业只能由提交该作业的用户查询。

### 4.2 执行中响应

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "running",
    "analysis_question": "分析本周期质量风险，并说明主要异常和处理建议",
    "uns_node_ids": ["2066811253765967872"],
    "output_format": {"type": "standard"},
    "result": null,
    "error": "",
    "created_at": "2026-09-01T10:00:00",
    "started_at": "2026-09-01T10:00:02",
    "finished_at": null
  },
  "message": "success",
  "code": 200
}
```

### 4.3 标准模式成功结果

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "success",
    "result": {
      "analysis_report": "## 核心结论\n本周期质量风险主要集中在……",
      "charts": [],
      "tables": []
    },
    "error": "",
    "finished_at": "2026-09-01T10:01:20"
  },
  "message": "success",
  "code": 200
}
```

`charts` 和 `tables` 使用数据洞察现有标准产物结构。

### 4.4 文本模式成功结果

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "success",
    "result": {
      "content": "本周期质量风险主要集中在甲产品的三个 OOS 批次……"
    },
    "error": ""
  },
  "message": "success",
  "code": 200
}
```

### 4.5 JSON 模式成功结果

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "success",
    "result": {
      "data": {
        "oos_batch_count": 3,
        "risk_level": "high",
        "summary": "风险集中在甲产品，建议优先复核对应批次的过程参数。"
      }
    },
    "error": ""
  },
  "message": "success",
  "code": 200
}
```

### 4.6 失败结果

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "failed",
    "result": null,
    "error": "分析所需的关键业务定义存在歧义，当前调用模式不支持用户澄清。",
    "finished_at": "2026-09-01T10:00:30"
  },
  "message": "success",
  "code": 200
}
```

这里 HTTP `200` 表示作业查询成功，`data.status=failed` 表示异步分析作业执行失败。

## 5. 取消洞察作业

### 5.1 请求

```http
POST /api/insight/workflow/jobs/{job_id}/cancel
Authorization: Bearer {token}
```

请求体为空。

### 5.2 响应

```json
{
  "success": true,
  "data": {
    "job_id": "6f0786266243472da8c62e05a9ac62c0",
    "status": "cancelled",
    "result": null,
    "error": "用户已停止本轮分析。",
    "finished_at": "2026-09-01T10:00:10"
  },
  "message": "洞察作业已停止",
  "code": 200
}
```

取消接口是幂等的。作业已经进入终态时，接口直接返回当前状态。

## 6. 常见错误

| HTTP 状态码 | 场景 |
| --- | --- |
| `400` | 参数缺失、输出协议非法、JSON Schema 不受支持或边界冲突、幂等键内容冲突、UNS 节点无法访问或转换 |
| `401` | 未获取到有效调用用户 |
| `404` | 作业不存在，或当前用户无权访问该作业 |
| `500` | 作业提交、状态同步或取消过程中发生服务异常 |

业务处理建议：

1. `400/401/404` 不做自动重试，先修正参数或权限。
2. `500` 可以结合相同 `Idempotency-Key` 做有限次数重试。
3. 作业状态为 `failed` 时，不要重新解释 `result`；记录 `error` 并交由工作流失败分支处理。
4. 作业状态为 `cancelled` 时停止轮询。

## 7. 推荐调用流程

```text
生成完整分析问题和 UNS 范围
    -> POST 提交作业并保存 job_id
    -> 每 2 至 5 秒 GET 查询状态
    -> queued/running: 继续等待
    -> success: 按 output_format 读取 result
    -> failed: 进入工作流失败分支
    -> cancelled: 结束处理
```
