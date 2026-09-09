# Runtime 工作记忆架构复核

## 结论

类型化工作记忆是正确方向，但不能被实现为第二份大 JSON 或另一条
Prompt 拼接链。它应是 Runtime 的本地控制面：为每一份原始信息声明
权威来源、身份、版本、作用域、依赖、生命周期和可见性，再由上下文
编译器在每次模型调用前生成当前工作的最小充分视图。

这保持一个统一洞察 Agent、一个完整分析语义和一个最终 Python 程序；
它不引入独立的指标规划 Stage，也不把 Preparation 的建议性业务信息
升级为限制 Runtime 表达的硬契约。

## 外部设计依据

- LangGraph 将线程状态视为跨节点共享的原始工作笔记，建议保存原始
  数据而不是格式化 Prompt；不同节点再根据自己的工作格式化它。
  <https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph>
- OpenAI Agents SDK 区分本地代码可用上下文与 LLM 可见上下文：前者
  可以是任意类型化对象，后者才是经选择后送入模型的消息。
  <https://openai.github.io/openai-agents-python/context/>
- Anthropic 将上下文视为有限注意力资源，强调每轮推理前循环整理、按需
  获取资料和收敛工具集合，而不是把历史、协议和工具输出持续累加。
  <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
- 数据洞察 Agent 的准确性主要由可读 Schema、业务语义、关系、指标定义
  和经过验证的规则提供；通用 Agent 指令不应该替代数据源语义层。
  <https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices>

## 当前代码事实

当前实现已经有正确的原始来源，但生命周期分散：

| 来源 | 当前内容 | 问题 |
| --- | --- | --- |
| LangGraph `messages` | 用户要求、模型调用、工具结果、代码、失败 | 是审计真相，但会随 ReAct 增长，不适合直接整段交给模型 |
| `PreparationResult` | Intent、选源、业务关系、Schema、业务血缘、Probe agenda | 是本轮准备事实的权威来源，但其 Prompt 视图和执行快照需要明确版本关系 |
| `short_term_memory` | Preparation 缓存、Schema 明细读取、Probe ledger、澄清/范围控制 | 使用裸字符串 key，当前不显式表达类型、来源、替代或失效规则 |
| Contract Registry | 查询、执行、产物、交互协议与 helper | 已有稳定 contract ID，但未作为 Runtime 记忆依赖统一审计 |
| 执行与 Evidence 持久化 | QuerySnapshot、执行结果、最终产物 | 应保持独立于模型视图，不能被上下文裁剪改变 |

现有 `runtime_working_context.py` 已开始把消息、Preparation/Schema 与
Contract 编成类型化索引，用于完整程序替代、Probe 源码归档和请求审计。
这只是过渡适配器，不是新的权威存储。

## 目标 RuntimeWorkspace

RuntimeWorkspace 是本地结构化状态的逻辑视图，不复制正文。每个条目至少
拥有以下元数据：

```text
kind, logical_key, revision, producer, source_ref,
scope, dependencies, status, supersedes, visibility_policy
```

`source_ref` 指回消息、Preparation 快照、短期状态或执行记录；正文只在
原始权威来源保存。`revision` 必须是内容或数据源快照版本，而不是消息
位置。未知输入保守保留，不进行基于文本的自动分类。

| 工作记忆族 | 权威生产者 | 正确生命周期 |
| --- | --- | --- |
| 交互 | 用户消息、同轮次澄清 | 用户新指令形成显式修订；不可被模型解释静默覆盖 |
| Preparation | Intent/Selection/Business Context | 作为 advisory 的分析范围与关系事实；当前轮结束失效或由重跑版本替代 |
| Schema | 选定数据源快照、按需 Schema reader | 同一数据源同一快照可按字段并集扩展；快照版本改变则失效 |
| 过程协议 | Contract Registry、工具定义 | 由工作状态、输出模式、数据源能力决定；同版本只渲染一次 |
| 查询事实 | QuerySnapshot、Probe ledger | 查询定义与每次执行观察分离；不同口径/总体不能 last-write-wins |
| 程序 | `submit_analysis_program` | 新完整程序替代旧完整程序；patch 依赖其基程序，不能伪装成完整版本 |
| 诊断 | 执行失败、修复目标、无数据控制 | 后续执行成功只能解决明确覆盖的失败，不能将历史失败整体标为已解决 |
| 产物 | 报告、表格、图表、Evidence | 与程序和执行 ID 绑定，供后处理和追问使用，不作为每次代码生成上下文 |

业务术语、指标口径、默认时间定义和可验证的业务规则属于长期语义层或
数据源 Schema 语义层，不属于一次 Runtime 的临时记忆。将它们只写进
总 Prompt 会使其不可验证、不可版本化，也会与用户当前问题混淆。

## 上下文编译规则

每次模型调用先由本地确定 `work_state`，再编译其依赖闭包：

| 工作状态 | 模型可见内容 | 不应重复送入 |
| --- | --- | --- |
| understand | 当前问题、交互修订、Preparation、Schema 索引、能力目录 | 完整代码、旧栈、全部 API 示例 |
| probe | understand 加相关 Schema 明细、查询协议、当前未解决技术事实 | 产物构造协议、已覆盖 Probe 源码 |
| implement | 当前洞察范围、必要 Schema、已确认查询事实、完整执行与产物协议 | 被替代程序、旧错误详情、重复协议 |
| repair | 当前程序、最近失败、仍未解决事实、完整实现依赖 | 已解决失败的栈、旧完整程序、与故障无关的 Probe 输出 |
| waiting | 持久化 Workspace 与转录，等待用户输入 | 不调用模型 |

工作状态是本地控制状态。模型可以声明当前业务焦点、请求已经登记的资料
或决定是否 Probe，但不能单独决定绕过必需协议、删除数据源范围或丢弃
澄清事实。每个请求视图必须能由 Workspace 确定性重建。

## 不应采用的方案

- 不建立“所有信息都塞入一个通用 memory JSON”的副本。
- 不通过 LLM 对每条历史消息分类、摘要和决定删除；这会增加调用、引入
  不稳定性，并会将审计事实变成概率性结果。
- 不用线性硬 Stage 分割洞察语义；Probe、实现、修复仍在同一 Runtime
  所有权下，最终生成一份完整 Python 分析程序。
- 不用 token 预算反向裁剪业务数据源、完整必需协议或用户明确范围。
- 不把“当前未显示给模型”误当成“原始事实被删除”。

## 迁移计划

1. 固化当前类型索引与请求投影的单测：程序替代、patch 链、Probe 事实、
   追问、澄清恢复、混合数据源、外部 JSON 输出都必须不丢能力。
2. 为 `short_term_memory` 的既有生产者增加薄适配器：Preparation 缓存、
   Schema detail、Probe ledger、范围/澄清控制分别登记类型与作用域；
   仍沿用现有正文存储，避免双写。
3. 将上下文编译器改为只从 RuntimeWorkspace 读取“当前状态的依赖闭包”，
   逐步移除基于消息顺序、字符串 key 或历史轮数的隐式裁剪。
4. 为每个数据源 Schema 快照和按需字段读取提供版本/失效语义；不得因为
   同名字段或相似文本错误合并。
5. 在真实回归前建立上下文审计：每次请求记录类型计数、依赖 ID、来源版本、
   原始/视图 token、是否存在未解释的重复或缺失。
6. 用现有会话矩阵做端到端验收：不仅检查成功率，还检查运行次数、上下文
   峰值、报告表达力、计算正确性、证据完整性和追问/澄清恢复。

## 验收不变量

- 原始消息和执行记录不因上下文优化被改变。
- 同一 Workspace 与同一工作状态产生可复现的模型视图。
- 追问 `context_mode`、输出格式、澄清模式和数据源能力不会因投影丢失。
- 同一查询定义可去重，不同范围、不同总体、不同执行观察不能错误合并。
- 任何被卸载的源码都已有独立、结构化、可追溯的事实替代物。
- Runtime 的资料按需加载不改变用户问题、Preparation 范围或正式分析的
  完整洞察语义。
