# Runtime Work Packet 设计

## 目标

本轮改造不增加新的洞察 Stage，也不把指标、查询、图表、表格或报告语义拆给
不同 Agent。Runtime 仍由同一个洞察 Agent 在完整语义下生成和修复一份正式
Python 分析程序。

改造只解决控制面问题：每次模型请求必须明确知道当前在做什么，本地必须据此
稳定装载恰好需要的上下文，旧消息不能再次解释或改变当前工作状态。

## 单一控制状态

每个分析任务持有一个小型 `RuntimeWorkPacket`。它只保存生命周期状态、版本和
权威内容的引用，不复制完整 Schema、Probe 输出、Python 源码或分析产物。

主要字段包括：

- `workspace_id/revision/phase/reason`：当前任务和状态修订。
- `focus/resume_phase/resume_focus`：当前聚焦任务以及临时 Probe 的返回点。
- `material_revision/material_manifest`：当前材料计划版本。
- `active_datasource_ids/active_fields_by_datasource`：当前 Schema 工作视图范围。
- `probe_revision/program_revision/diagnostic_revision`：事实、程序和诊断版本。
- `active_program_call_id/active_execution_id/latest_result_call_id`：权威结果引用。

完整内容继续由既有生产者保存：Preparation 快照、Schema workspace、Probe fact
ledger、执行记录、QuerySnapshot 和结果产物分别保持自己的权威来源。

## 状态流转

```text
understand -> plan -> implement -> review -> completed
                    \-> probe -> implement
implement/repair -> probe -> 原调用阶段
implement -> repair -> review
任一有效业务判断点 -> waiting_clarification -> plan
```

模型只能调用当前状态允许的工具。本地 middleware 在工具真实返回后，通过纯
reducer 更新 Work Packet。模型文字、旧工具调用以及历史消息都不能推进状态。

Probe 是临时工作：进入时同时保存调用前的阶段、焦点和材料范围；成功后完整
恢复。这样“确认日期格式”不会替代“完成全量产品分析”这一正式目标。

## 上下文编译

每次请求按 `Work Packet.phase` 确定性完成以下工作：

1. 选择该状态需要的常驻契约和场景契约。
2. 从 Work Packet 的当前材料范围投影 Schema 工作视图。
3. 从既有权威存储装载相关 Probe、Query、程序和最新诊断。
4. 从审计消息中保留当前动作所需的调用结果，归档被替代的程序和错误。
5. 仅暴露当前状态允许的工具。

`plan` 状态接收完整的紧凑数据源索引和业务血缘，但不按血缘自动展开完整
Schema。模型可通过数据源上下文读取工具取得确切字段详情；只有这些显式读取
以及最终材料计划选中的范围，才进入 Probe、实现和修复视图。

材料计划选定字段后，后端可直接从权威执行快照投影其精确 Schema，无需模型
为了“确认已加载”再调用一次读取工具。读取工具仍用于计划阶段理解字段详情，
以及执行阶段发现材料计划之外的新依赖。

Work Packet 随 ToolMessage 保存在无损审计轨迹中，并同步进 Runtime 短期状态，
用于外层恢复和同轮次澄清；在发给模型前必须剥离内部载荷。模型只看到当前
`work/focus` 和完成该工作的事实、规范与工具，不读取控制面实现细节。

## 兼容与边界

- 新任务在进入 Runtime 时立即创建 Work Packet。
- 旧的在途轨迹缺少 Work Packet 时，允许一次性从历史兼容恢复；兼容包不会
  成为新任务的正常状态来源。
- 当前继续复用 LangGraph ReAct、原执行工具、同轮次澄清、外部分析接口、
  后置 Evidence、本地执行和洞察任务能力。
- 本轮不改变 Preparation 职责、不缩减数据源范围、不新增指标规划协议，也不
  通过 token 上限裁剪分析所需的核心 Schema 或能力契约。

## 验收不变量

1. 显式 Work Packet 必须覆盖与其冲突的陈旧历史状态。
2. 相同 Packet 和权威来源必须产生相同工作视图与工具集合。
3. Probe 成功后必须返回原阶段、原分析焦点和原材料范围。
4. 修复只使用当前程序及最新未解决诊断，旧错误不能改变阶段。
5. 澄清挂起和恢复必须保留 Preparation、Probe 事实和 ReAct 审计轨迹。
6. 内部 Work Packet 不得出现在模型上下文中。
7. 功能验收仍以真实会话的准确性、稳定性、报告质量和整体耗时为准，不能用
   单测通过或输入 token 下降替代。
