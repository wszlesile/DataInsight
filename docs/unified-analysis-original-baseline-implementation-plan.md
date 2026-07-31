# 基于原版的统一洞察架构落地计划

## 1. 推进基线与分支

- 实现基线：`release/V1.08.00.00-C-refactor`
- 基线提交：`3f52c94`
- 推进分支：`codex/unified-analysis-runtime-v2`
- 历史实验参考：
  - `codex/unified-analysis-runtime`
  - `codex/unified-analysis-runtime-rebuild`

历史实验分支只用于提取已验证的能力和测试，不做整体合并。

## 2. 推进方法

采用“纵向闭环、逐批迁移、每批可回滚”的方式：

1. 每批只解决一个明确问题。
2. 每批先写行为测试，再迁移最小实现。
3. 每批同时验证流程成功、报告质量、证据链和耗时。
4. 发现表达力或稳定性回退时停止下一批，不用 Prompt 禁止项掩盖问题。
5. 环境配置 `src/.env` 和 `src/api/SuposkernelApi.py` 永不纳入提交。

## 3. 阶段零：冻结原版行为样本

### 工作

1. 记录原版真实执行流程、Prompt 组成和工具生命周期。
2. 选取固定回归会话，覆盖：
   - 单关系型数据源
   - 多关系型数据源
   - 时序数据源
   - 本地文件
   - 问数
   - 综合分析
   - 无数据
   - 追问与刷新
3. 保存每个样本的：
   - 选源范围
   - LLM 与工具调用次数
   - 总耗时
   - 报告、图表和表格
   - Evidence/Trace 情况
4. 建立人工报告质量检查表。

### 通过门槛

- 能复现原版的报告表达力。
- 已知原版失败点有明确记录，不能把概率性跑通当作稳定基线。

## 4. 阶段一：Preparation 最小闭环

### 1A. Intent Analysis

只输出供选源使用的业务意图摘要，不输出分析模式、指标、报告结构或执行约束。

### 1B. 渐进式选源

1. 本地检索先生成高召回候选。
2. LLM 根据业务意图和候选概要请求必要的完整 Schema。
3. 最终选择相关数据源。
4. 候选阈值不能由正式 Prompt 容量反向决定。
5. 一个数据源的 Schema 一旦披露必须完整，不裁剪字段。

### 1C. ER 关系补充

1. 在 Selection 的同一 Preparation 语义中判断数据源关系。
2. Schema 已明确声明的关系直接记录。
3. 只有关系选择确实依赖枚举、格式或覆盖事实时，才调用受限 Probe API。
4. 无法确认的关系记录为 unresolved。
5. 关系写回数据源 Schema，不形成额外大对象。

### 1D. 降级

Preparation 任何非授权类错误都不能终止正式分析。降级顺序：

1. 已选择且 Schema 完整的数据源；
2. 高召回候选数据源；
3. 原版会话授权数据源快照。

### 通过门槛

- 单数据源简单问题最多增加一次 Intent/Selection 调用，或通过合并调用避免额外开销。
- 多数据源问题选源召回率不低于原版。
- 正式 Agent 收到完整选中 Schema。
- Preparation 不生成失败报告，不冻结洞察语义。

## 5. 阶段二：正式上下文投影

### 工作

1. 保留原版 `sys_prompt.md` 为语义原型。
2. 按以下模块重组，但不先删能力：
   - 角色与输出目标
   - 业务规则
   - 数据源 Schema
   - 数据访问协议
   - Python 代码规范
   - Probe/No Data
   - 产物与 Evidence
   - 执行修复
3. 查询能力按实际选中类型加载：
   - local file
   - relational SQL
   - time series
4. 正式上下文只接收：
   - 当前问题
   - 必要会话上下文
   - 选中且关系增强后的完整 Schema
   - 简短 advisory intent
   - 当前 Runtime/API 能力
5. 建立上下文块预算监控，只压缩历史和重复说明，不裁剪核心 Schema。

### 通过门槛

- 原版数据访问能力无丢失。
- 正式 Prompt 中同一规则只有一个权威来源。
- 报告质量不低于阶段零样本。
- 固定上下文大小和模型首响应耗时可观测。

## 6. 阶段三：Probe、No Data 与修复连续性

### 工作

1. 保持原版 `execution_intent = probe/analysis` 主模型。
2. 保持 Probe 与正式分析位于同一 Agent ReAct 上下文。
3. 将 Probe 反馈结构化为：
   - probe purpose
   - query scope
   - observed facts
   - unresolved facts
   - candidate values/formats
   - no-data evidence
   - next allowed action
4. Query constraints 在 Probe 确认后进入本轮短期记忆，正式分析保持一致。
5. 修复反馈必须包含：
   - 错误类型
   - 原始诊断
   - 精确失败位置
   - 保持不变的分析目标和已成功事实
6. 删除基于特定字段、状态值和单位的修复启发式。

### 通过门槛

- Probe 不计算正式指标。
- Probe 成功后下一次正式代码能够消费其结构化事实。
- 同一错误不连续重复。
- 确认无数据时生成完整无数据洞察报告。
- 修复后报告内容和图表不因无关错误被删除。

## 7. 阶段四：Metric/Evidence Runtime

此阶段必须拆成独立小批次，不能一次替换原版结果保存协议。

### 4A. QueryResult 注册

- 正式查询与派生变换注册为可追溯 QueryResult。
- 保留原版所有数据源查询能力。

### 4B. Metric 对象

- 聚合、分组、identity 和派生计算通过 Runtime API 注册。
- MetricDefinition 保存真实查询、字段、过滤、分组、公式和单位。
- 不同 metric key 声称相同计算边界时明确拒绝，不能静默复用另一个 Metric。

### 4C. Evidence 自动生成

- 每个 Metric 自动生成唯一 Evidence。
- Evidence 的技术谱系由 Runtime 生成。
- 面向用户的计算说明由结构化业务名称、操作和口径生成，不暴露 Runtime 术语。

### 4D. Trace 自动绑定

- 报告使用 MetricTrace。
- 图表按数值字段绑定 Metric。
- 表格按业务数值列绑定 Metric。
- grouped Metric 的成员通过 selector 定位。
- 禁止手写 evidence key/ref。

### 4E. 兼容移除

新路径稳定后删除：

- 手写 EvidenceDefinition 路径
- 手写 metric_evidence_bindings
- Markdown 数字扫描
- legacy Evidence 兼容路径

### 通过门槛

- metric 数量与唯一 evidence 数量一致。
- 不同计算签名不能引用同一 Evidence。
- 同一 grouped Metric 不按行或单元格复制 Evidence。
- 报告、图表、表格点击值与 Evidence 当前值、单位完全一致。
- 本地重新执行能重新生成同样的绑定关系。

## 8. 阶段五：同轮次澄清

### 工作

1. 定义结构化 clarification request。
2. SSE 推送快捷选项和自由文本输入要求。
3. 当前 task 进入 waiting 状态，不标记失败。
4. 用户回复后恢复同一 turn 和原 Agent 目标。
5. 澄清仅用于 Schema 与 Probe 都无法消除的实质歧义。

### 通过门槛

- 不新增用户轮次。
- 可取消、可恢复、可超时处理。
- 常见业务集合词能由 Schema/Probe 解释时不打扰用户。

## 9. 阶段六：全量回归与性能验收

### 回归顺序

1. 聚焦单元测试。
2. Runtime 组件测试。
3. 固定会话正式 Runtime mock Preparation 回归。
4. Preparation 独立回归。
5. 端到端固定会话回归。
6. 追问、刷新、本地执行、终止和历史展示回归。

### 质量评价

每份报告人工检查：

- 是否回答真实业务问题；
- 是否有核心结论；
- 是否有数值支持；
- 是否包含趋势、结构、对比、排名或异常中真正相关的部分；
- 是否有业务解释；
- 建议是否由事实支持；
- 是否出现数据源 ID、Metric、Runtime、字段名等技术语言；
- 是否比原版更薄或更模板化。

### 性能评价

记录各步骤：

- LLM 调用次数
- 输入/输出 Token
- 首响应时间
- Stage 时间
- execute_python 次数
- 同类重试次数
- 总耗时

没有指标改善时，不以增加超时作为完成标准。

## 10. 首批实施范围

第一批只实施阶段零和阶段一：

1. 原版回归基准记录。
2. 非终止、非强契约的 Preparation。
3. 完整 Schema 的选源结果。
4. ER 关系内聚。
5. 原版统一 Agent 完全不改。

只有第一批证明：

- 选源更准；
- 正式 Agent 表达力不下降；
- 总耗时可接受；
- 原版 Probe/No Data/修复行为未改变；

才进入正式上下文和 Runtime 改造。

