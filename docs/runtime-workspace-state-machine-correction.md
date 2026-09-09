# Runtime 工作状态与记忆管理收敛方案

> 状态：已停止作为实施依据。本文提出的强制材料阶段、实现态禁止补载、
> 必须替换 create_agent 为 Driver 等结论未经充分验证；这些限制可能再次
> 割裂语义或阻断正常分析。后续以 runtime-focus-memory-redesign.md 的复核
> 方案为讨论和设计依据。本文保留用于记录实验教训。

## 背景

比较基线为 `be9d3fd7`。基线 Runtime 保持统一洞察语义：同一个 ReAct
Agent 在完整 Schema、查询能力、Probe 反馈和执行反馈下生成并修复一份
Python 分析程序。它存在单次上下文偏大、模型容易在查询/计算/产物规范间
分散注意力的问题，但状态语义很简单：尚未执行、Probe、正式执行、修复或
完成。

当前工作区尝试以 `select_runtime_work`、`plan_runtime_materials`、类型化
消息索引和状态化 Prompt 视图解决这个问题。真实回归表明该实现没有形成
可靠的渐进式工作机制：模型既要决定分析，又要决定控制状态；本地再从完整
消息历史倒推“当前状态”。一次无效工具调用、旧工具消息或恢复反馈都可能
改变下一次的状态和上下文。会话 44/64 虽最终成功，真实任务耗时约 197 秒，不能
视为可接受的收敛结果。

本方案的目标不是重新拆分洞察所有权，也不是压缩用户的数据范围、Schema
或报告能力；而是把 Runtime 内部的控制流和工作记忆变成确定性基础设施，
让模型只做其真正需要做的业务判断和程序生成。

## 已确认的问题

### 状态不是单一事实来源

当前 `resolve_work()` 扫描全量 `messages`，从工具调用、工具结果、Probe
诊断和正式程序失败中推断 `understand/plan/probe/implement/repair`。状态
转移同时散落在：

- `RuntimeWorkingContextMiddleware.resolve_work`
- `plan_runtime_materials` 的输出
- `probe_analysis_data` 的工具返回
- `submit_analysis_program` 的失败诊断
- `invoker` 的 `short_term_memory`

这意味着状态不是一个带版本的对象，而是历史文本的解释结果。模型的控制面
选择一旦不理想，下一次调用就可能收到不属于当前工作的资料或工具。

### 记忆索引不是工作区

`WorkingMemoryIndex` 目前是从消息和 Context 即时构建的元数据索引。它有
助于排除部分旧程序，但不能声明“当前 Runtime 的权威目标、当前材料计划、
当前 Probe 生命周期、当前程序版本和最近失败”分别是谁。正文仍由消息、
`short_term_memory`、Preparation 快照、Schema workspace 和恢复提示拼接。

因此它不能可靠实现替代、失效、依赖闭包或最小充分投影。

### 模型承担了本地应承担的控制职责

`select_runtime_work` 让模型选择 `plan/probe/implement`；`plan_runtime_materials`
又让模型决定数据材料。模型应能表达业务焦点和技术未知，但不应拥有绕过或
反复进入 Runtime 工作状态的权限。控制状态应由本地根据可验证的工作结果
推进，模型只能提交受结构化校验的“下一步业务需求”。

### 视图由历史拼接而非状态依赖闭包编译

当前实现同时投影紧凑 Schema、材料计划、业务血缘、已读 Schema、Probe
ledger、恢复提示和工具转录。即使每一块单独有裁剪，也难以证明某次调用恰好
包含当前工作所需且没有重复的内容。状态和视图缺乏显式版本绑定。

### 外层恢复循环会丢失 Agent 内状态

`invoker` 的同步和流式路径都维护 `agent_round_messages`，并在每个外层
round 重新调用 Agent、重新构造 `CustomContext`。因此 LangGraph Agent state
只在单个内部 ReAct 图执行期间存在；一旦进入外层修复 round，当前实现又只能
从完整转录和 `short_term_memory` 重建状态。

这说明不能只在 `RuntimeWorkingContextMiddleware` 增加 Agent state 字段。
Workspace 的权威实例必须由 `ConversationRunContext`/任务运行时持有，并贯穿
外层恢复循环；或者由一个显式 Runtime Driver 自己调用模型和工具。否则会存在
两层独立 ReAct 控制器，状态再精细也会在边界丢失。

## 设计原则

1. **一个 Runtime、一个洞察语义、一个最终 Python 程序。** 内部调用可以
   聚焦，但任何实现/修复调用都能得到完成该程序所需的完整业务语义。
2. **本地控制流，模型业务判断。** 本地决定阶段、可见工具、阶段入口与出口；
   模型决定业务资料需求、是否存在技术未知、查询和分析程序。
3. **Workspace 是唯一控制事实来源。** LangGraph 转录仍是完整审计记录，
   但不再承担状态解释职责。
4. **按依赖闭包投影，不按历史顺序拼接。** 每次请求由当前 Workspace 版本、
   当前阶段和声明依赖确定性编译；被替代程序、已解决失败和无关 Probe 不进入。
5. **不以容量限制削弱分析能力。** 完整 Schema/Preparation/历史事实保留在
   Workspace，模型当前视图可按工作投影；缺资料有明确补载路径，不能静默丢弃。
6. **Probe 保持基线职责。** Probe 仍可验证查询口径、字段格式、数据覆盖和
   无数据；它不是前置选源的替代，也不计算正式指标。
7. **澄清是受控终态。** 只有影响答案且无法用当前 Schema、Preparation 或
   Probe 确认的业务歧义才进入 `waiting_clarification`；外部 API 可关闭该能力。

## 目标 RuntimeWorkspace

每一轮 Runtime 创建一个显式、可序列化的 `RuntimeWorkspace`，由
`ConversationRunContext` 持有并按需持久化到任务运行态。Agent state 只能是
该 Workspace 的当前请求投影，不能成为跨外层恢复循环的权威来源。状态不再
从消息历史反推：

```text
workspace_id, revision, phase, analysis_goal_revision,
preparation_revision, material_revision, probe_revision,
program_revision, diagnostic_revision, output_contract_revision,
phase_attempt, pending_requirement, phase_reason
```

正文由权威来源保存，Workspace 只保存 ID、版本、依赖和生命周期：

| 记录 | 权威来源 | 替代规则 |
| --- | --- | --- |
| `analysis_goal` | 用户问题 + 同轮次澄清 | 用户补充形成新 revision |
| `preparation` | Intent/Selection/Business Context 输出 | 当前轮固定，重跑替代 |
| `material_manifest` | 规划调用的结构化结果 | 同 goal/preparation revision 下新 manifest 替代旧版 |
| `schema_projection` | 执行快照及 Schema reader | 按 datasource+schema revision+field set 合并 |
| `probe_ledger` | Probe 真实执行结果 | 按问题/数据源/查询口径/字段范围更新，不按消息位置累加 |
| `program` | 正式程序工具调用 | 完整程序替代旧完整程序；patch 依赖明确 base |
| `diagnostic` | 最近失败执行 | 后续成功或明确覆盖的修复替代 |
| `artifacts` | 成功执行/后置 Evidence | 终态，不回灌到代码生成上下文 |

每条记录有 `kind/logical_key/revision/producer/source_ref/dependencies/status`。
这与现有类型化索引兼容，但索引将升级为 Workspace 的持久控制面，而非每次
调用的消息扫描器。

## 受控内部工作流

这不是对外新增 Workflow Stage；它是统一 Runtime 内部的有限状态机。

### Runtime Driver 是唯一控制器

不能继续依赖通用 `create_agent` 的隐式多工具 ReAct 循环，再在外层用
`agent_round_messages` 恢复。正确实现是一个由 `invoker` 调用的显式
`RuntimeDriver`：

```text
while workspace.phase is not terminal:
    packet = ContextCompiler.compile(workspace, authoritative_sources)
    response = model.invoke(packet.messages, tools=phase_tools(workspace.phase))
    action = validate_single_phase_action(response, workspace.phase)
    tool_result = execute(action)
    workspace = transition(workspace, action, tool_result)
```

这里的“一次”指一个聚焦工作单元的一次模型决策和一次工具动作，不限制完整
Runtime 内部存在多个单元。模型不再在同一次泛化 Agent graph 内连续选择多项
控制工具；任何工具结果都会结束当前单元，Driver 写入新 revision，再构造下一
个包。这样保证：

- `materialize` 只会读取精确 Schema 或提交材料清单；
- `probe` 只会提交 Probe；
- `implement` 只会提交完整正式程序；
- `repair` 只会提交修复程序，或在本地诊断明确许可时提交 Probe；
- 无法消除的业务歧义只会请求澄清并进入挂起状态。

现有 `probe_analysis_data`、`submit_analysis_program`、澄清、QuerySnapshot、
后置 Evidence 和取消检查仍复用；变化只是由 Driver 包装其调用，禁止它们触发
一个不受控的下一次模型调用。同步和 SSE 路径共同调用同一个 Driver，不能再
各自维护一套恢复循环。

```text
prepare_context
  -> materialize
  -> probe (可选)
  -> implement
  -> repair (可循环)
  -> completed
                  \-> waiting_clarification (可选)
```

### `prepare_context`：本地进入

本地从 Preparation、当前问题、已持久化澄清和输出契约创建 Workspace。
不调用模型，不改变业务意图。

### `materialize`：模型只做材料需求判断

输入：问题、Preparation 的完整紧凑业务血缘、Schema 索引、能力目录。

输出为受 schema 校验的 `MaterialManifest`：业务事实对应的数据源、精确字段、
已确认关系，以及无法由 Schema 判断的技术未知。它不定义指标、结论、图表
或报告模板，也不拥有改变分析范围的权限。

本地校验所有数据源和字段归属后，将其写为 `material_revision`；本地决定：
- 无技术未知：进入 `implement`；
- 有可 Probe 的未知：进入 `probe`；
- 有影响答案但不可 Probe 的业务歧义：进入 `waiting_clarification` 或外部
  API 的受限说明路径。

模型不调用 `select_runtime_work`，也不通过自然语言改状态。

### `probe`：模型只生成一次焦点明确的探测程序

输入：`MaterialManifest`、其精确 Schema 投影、Probe 协议和未解决技术未知。
可用工具仅 `probe_analysis_data`（以及允许时澄清）。

Probe 的结构化结果由本地写入 `probe_ledger`，然后本地根据结果决定：
- 事实足以支持正式计算：进入 `implement`；
- 同一未知未解决但有明确修复目标：留在 `probe`，带最新诊断；
- 业务语义仍无法确认：进入 `waiting_clarification`；
- 已达到无数据证明条件：进入实现一份无数据分析产物的路径。

### `implement`：模型只实现完整程序

输入为唯一的 `ImplementationPacket`：

```text
analysis_goal + preparation semantic context + material manifest
+ exact schema projection + confirmed probe facts
+ output contract + 当前数据源能力对应的实现/产物协议
```

工具仅 `submit_analysis_program`（及允许时澄清）。无工作状态选择、无 Probe、
无 Schema reader、无参考读取。实现请求不拼接旧控制工具消息、旧 Probe 源码、
被替代程序或过期失败。

程序提交失败时，本地写入 `diagnostic`；由诊断类型决定进入 `repair` 或有限的
`probe`，模型不自行重开任意状态。

### `repair`：以程序版本和诊断为中心

输入：`ImplementationPacket`、当前完整程序、最近一次未解决诊断、相关的
Probe/Query 事实、故障相关协议增量。旧错误和旧完整程序不出现。

工具默认只有 `submit_analysis_program`。只有诊断明确标记“缺少可 Probe 的
技术事实”时，本地才让该轮具备 `probe_analysis_data`；不是模型随意选择。

### `waiting_clarification` 与 `completed`

两者是本地终态。前者等待用户在当前轮补充，补充后更新 `analysis_goal_revision`
并从 `materialize` 重启。后者保存最终产物、QuerySnapshot 和后置 Evidence；
不会再请求模型。

## 上下文编译器

编译器输入固定为：`Workspace + phase + source revisions`，输出为本次调用唯一
的 `ContextPacket`。每个包记录 `workspace_id/revision/phase/dependency_ids` 和
按类型计数，便于审计重复和缺失。

| 阶段 | 常驻内容 | 阶段材料 | 不应出现 |
| --- | --- | --- | --- |
| materialize | 用户问题、澄清、Preparation 紧凑语义、Schema 索引、材料协议 | 能力目录 | 代码/图表协议、Probe/修复历史 |
| probe | analysis goal、manifest、精确 Schema、Probe 协议 | 未解决技术未知 | 正式产物协议、旧 Probe 源码 |
| implement | ImplementationPacket、实现协议、当前输出协议 | 必要 helper 示例 | 资料选择工具、旧控制记录、旧错误 |
| repair | ImplementationPacket、当前程序、最新诊断、相关事实、修复协议 | 诊断关联的 helper | 已解决错误、历史程序、无关 Schema |

核心实现/产物协议是 `implement` 的必需上下文，不能为追求 token 数而拆掉；
查询类型、文件/时序/关系型特有语法和可选示例由 `MaterialManifest` 的能力
集合按需加入。Schema 由索引逐步转成精确字段投影，但完整快照永远留在本地，
并可由明确的 manifest 修订补载。

## 迁移顺序

1. 先建立 Runtime Driver 与 `ConversationRunContext.runtime_workspace`；Driver
   负责跨外层调用保留 revision、phase 和 source references。冻结并移除
   `resolve_work()` 对完整消息历史的状态反推，只允许本地 transition 函数修改
   `phase`。
2. 用一个受 schema 校验的 `materialize` 调用替代
   `select_runtime_work + plan_runtime_materials` 两个控制工具。保留业务材料
   选择能力，不在这里提前规划指标/报告。
3. 将现有 Probe ledger、Schema workspace、程序/执行诊断迁移为带 revision
   的 Workspace 记录，先兼容旧 `short_term_memory` 读取，随后删除裸 key
   反向推导。
4. 将 `RuntimeWorkingContextMiddleware` 改成纯 ContextPacket 编译器：不再
   决策状态，不扫描历史决定路由，不隐藏/显示由模型临时选择的控制工具。
5. 为每个 phase 建立不变量测试：固定 Workspace 必须得到稳定视图；无关历史
   不能改变状态；完成 Probe 后只能由本地进入实现；修复只看到最新程序与
   最新未解决诊断；澄清和外部调用模式保留。
6. 再做 `be9d3fd7` 同环境 A/B 回归，先验证单源简单会话，再验证多源、
   Probe、无数据、澄清、追问、时序、文件和关系型场景。验收同时记录调用
   次数、各 phase 输入 token、模型耗时、真实数值、报告表达和 Evidence。

## 明确不做

- 不用多个彼此割裂的洞察 Stage 替代统一 Runtime。
- 不让 Preparation 提前冻结指标、主辅角色、图表、报告章节或结论。
- 不因 token/超时裁剪完整数据源范围、用户问题、关键 Schema 或必要协议。
- 不以 Prompt 禁止语句或会话特例替代状态不变量和工具协议。
- 不把状态切换失败伪装为普通无数据报告或 fail report。
