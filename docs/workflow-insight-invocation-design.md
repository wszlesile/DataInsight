# 业务工作流洞察调用设计

## 目标

业务协同工作流可以在不创建用户可见空间和会话的情况下提交一次洞察分析。调用方只提供：

1. `analysis_question`：已经完成工作流变量插值的完整分析问题。
2. `uns_node_ids`：本轮允许使用的 UNS 节点范围。
3. `output_format`：`standard`、`text` 或带 JSON Schema 的 `json` 输出契约。
4. `analysis_preferences`：与交互式会话提示词语义一致的补充业务规则和表达偏好。

工作流调用复用现有 Preparation、统一 Runtime、Python 执行和结果持久化，不复制第二套分析 Agent。

## 调用边界

### 请求

```json
{
  "analysis_question": "分析本周期质量风险",
  "uns_node_ids": ["123", "456"],
  "output_format": {
    "type": "json",
    "schema": {
      "type": "object",
      "properties": {
        "oos_batch_count": {
          "type": "integer",
          "description": "本周期 OOS 去重生产批次数"
        },
        "summary": {
          "type": "string",
          "description": "结合指标得到的业务结论"
        }
      },
      "required": ["oos_batch_count", "summary"],
      "additionalProperties": false
    }
  },
  "analysis_preferences": "重点关注异常原因和可执行建议"
}
```

`Idempotency-Key` 和 `X-Request-ID` 使用请求头承载，不进入业务参数。

### 输出模式

- `{"type":"standard"}`：现有 Markdown 报告、图表和表格。
- `{"type":"text"}`：纯文本洞察结果，不要求图表和表格。
- `{"type":"json","schema":{...}}`：真实 JSON 对象，必须通过调用方 Schema 校验。

JSON Schema 首版支持本地可验证的安全子集，禁止远程引用和递归引用。调用方应通过字段名、`description`、类型、枚举和必填项表达所需指标及含义。

## 执行策略

后端将调用场景归一为不可变策略：

```text
interactive:
  clarification_mode = same_turn（默认，可由请求关闭）
  evidence_mode = enabled

workflow:
  clarification_mode = disabled
  evidence_mode = disabled
```

关闭同轮次澄清时，Runtime 不暴露澄清工具，也不加载澄清工具协议。可由 Schema 或 Probe 解决的未知仍由 Runtime 自行解决；确实无法形成可靠答案时，工作流任务以 `semantic_ambiguity` 终止，不挂起等待用户。

工作流成功结果不执行后置 Evidence 编译，也不写入 Evidence 或产物引用。

## 内部虚拟上下文

每个调用用户拥有一个不可见的工作流内部空间，每个工作流作业创建独立隐藏会话和轮次。内部上下文只用于复用现有分析链路、执行审计和结果持久化，对外只暴露 `job_id`。

UNS 节点按调用用户权限解析并物化，随后绑定到隐藏会话。Preparation 和 Runtime 只能在调用方声明的 UNS 范围内工作，不调用语义中心扩大范围。

`analysis_preferences` 使用现有会话提示词服务写入隐藏会话，保证其解释方式与交互式分析一致。

## 生命周期

```text
POST /api/insight/workflow/jobs
GET  /api/insight/workflow/jobs/{job_id}
POST /api/insight/workflow/jobs/{job_id}/cancel
```

状态为 `queued/running/success/failed/cancelled`。提交接口返回 `202`，查询接口同步底层分析任务状态并返回最终输出。相同用户和相同 `Idempotency-Key` 返回原作业，不重复创建分析。

## 兼容原则

1. 现有前端不传执行策略时，保持 `standard + same_turn + evidence enabled`。
2. 工作流输出契约只改变产物形态，不拆分 Runtime 的洞察语义所有权。
3. JSON Schema 是调用方明确提供的结果契约，不是 Preparation 自主规划指标。
4. 工作流隐藏空间不能出现在现有空间、会话、收藏和洞察任务列表。
5. 工作流不生成 Evidence，但仍保留查询快照、实际使用数据源和 Python 执行记录用于审计。

## 验收

1. 现有交互式分析默认行为、澄清和 Evidence 零回退。
2. 前端分析任务可显式关闭同轮次澄清。
3. 工作流无需空间和会话 ID 即可提交。
4. UNS 权限不足、节点不存在或 Schema 无法满足时返回明确错误。
5. 文本模式没有空图表/表格要求。
6. JSON 模式返回对象并通过 Schema；缺失必填字段、类型错误和额外字段均不能成功。
7. 工作流成功执行后不存在对应 Evidence 记录和 Evidence refs。
