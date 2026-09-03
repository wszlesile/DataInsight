# Runtime 语义准确性与稳定性审计

> 最新状态见 [2026-09-02 基线复核](runtime-baseline-audit-20260902.md)。本文保留前期问题与演进记录；前期已撤除的语义编译/审核调用不能作为当前代码事实。当前仍未通过整体验收。

## 1. 审计范围

本次审计以当前分支已提交版本 `be9d3fd7` 为基线，检查工作区待提交改动对以下目标的实际影响：

1. 信息充分时，统一 Runtime 直接生成准确、完整且有表达力的洞察分析；
2. 技术未知由 Probe 在同一 ReAct 上下文内解决；
3. 只有关键业务定义存在互斥解释且无法由上下文、Schema 或 Probe 唯一确定时，才进行同轮次澄清；
4. 澄清答案提高计算准确性，但不形成指标计划、报告模板或第二个洞察语义所有者；
5. 代码错误反馈能够在同一 ReAct 上下文中一次修复；
6. Evidence 保持后置处理，不侵入正式分析语义；
7. 稳定性、性能和报告表达力不能相互置换。

审计依据包括当前代码差异、Runtime Prompt 与上下文组装、任务挂起恢复、Probe/Analysis 修复链、后置 Evidence，以及 226 环境会话 66 轮次 99 的真实复跑。

## 2. 真实复跑结论

最近一次复跑总耗时约 `302.75s`，Preparation 约 `46s`，随后完成一次 Probe、两次正式代码提交和后置 Evidence。流程最终成功，Evidence 状态为 complete，但核心指标仍然错误：

- 实际生成的一次合格率：`672 / 744 = 90.32%`；
- 已复核的正确口径结果：`692 / 744 = 93.01%`。

错误代码用每批 `MIN(test_time)` 选一条记录代替“每批全部初检记录”，把业务角色错误替换成时间顺序代理。新增语义审核第一次以“分子、分母总体未证明一致”为由拒绝代码；模型显式求交集后，审核通过，但错误的 `MIN(test_time)` 仍被保留。因此当前审核既产生误报，又没有识别真正的语义错误。

这不是缺少一条提示或一个 AST 规则，而是语义所有权和上下文流向发生了结构性分裂。

## 3. 根因

### 3.1 本轮引入：Runtime 内部出现额外语义解释者

这里没有新增独立的 Workflow Stage 或 LangGraph Node。目标流程仍然是同一个
Runtime ReAct 内完成语义确认、Probe、同轮次澄清、恢复和正式分析。问题在于待提交
实现又在这个流程的入口与工具执行边界加入了两个额外 LLM 子调用：

当前待提交实现形成了以下链路：

```text
用户澄清原文
  -> Semantic Decision Compiler LLM
       -> 总体、粒度、记录范围、量词、分子分母、时间归属、公式、Probe 要求
  -> 统一 Runtime ReAct 生成代码
  -> Semantic Adherence Reviewer LLM 审核代码
  -> 本地 AST/dataflow 再决定是否阻断
```

因此，问题不是“多了两个 Stage”，而是同一 Runtime 链路内出现了多个语义所有者。
编译器输出已经实质构成指标计算边界；审核器又重新解释一次代码语义。三个模型调用
可能形成不同理解，且后两者均无法提供确定性证明。这与“统一 Runtime ReAct 是唯一
洞察语义所有者”的设计冲突。

该链路还带来两个直接问题：

- 每个含澄清事实的复跑增加一次编译调用，每次正式代码提交再增加一次审核调用；本轮仅这部分就增加约 `70-100s`；
- 审核不可用时 fail-open，审核可用时又可能错误阻断，无法同时保证准确性与稳定性。

### 3.2 本轮引入：Probe 字段硬门槛仍是语法启发式补丁

最新实现把编译器推断出的 `candidate_fields` 交给本地校验，并通过扫描 Probe 代码字符串判断字段是否“已观察”。这不能证明：

- Probe 查询成功；
- diagnostics 返回了该字段的真实值；
- 正式代码消费了观察结果；
- helper、变量或动态列表形式的合法 Probe 被正确识别。

它既可能放过失败 Probe，也可能拒绝合法写法，属于把语义问题继续转化为语法规则，不能作为下一版基础。

### 3.3 本轮引入：同一权威事实被重复注入

澄清答案当前同时进入：

1. Runtime 的 `<user-clarification-answers>`；
2. `CustomContext.authoritative_business_facts`；
3. Probe `request_retry(...).diagnostics.authoritative_business_facts`；
4. 正式代码审核输入。

Probe 反馈应只返回真实技术观察，不应再次复制业务事实。重复注入扩大了消息历史，也降低了最新工具观察和原问题的显著性。

### 3.4 基线已有：Prompt 预算并没有约束固定上下文

当前 Runtime 系统提示约 `87k` 字符，项目 token 估算约 `46k`。会话 66 轮次 99 的 9 个完整 Schema 约 `44k` 字符、`13k` token。两者合计约 `59k` token，已经远高于配置的 `24000` token Prompt 预算，且尚未包含：

- `analysis_semantic_context`；
- 会话补充规则；
- 澄清答案；
- Probe ToolMessage；
- 生成代码和修复反馈；
- 工具 Schema。

现有预算只裁剪可选记忆和历史，不处理固定系统块。结果不是 Prompt 被控制在预算内，而是历史和记忆经常被压缩为零，固定上下文继续超预算。这会同时损害首次代码准确性、追问承接和修复注意力。

### 3.5 基线已有：能力模块没有按需加载

架构文档要求根据已选数据源类型加载查询能力，但 Runtime 实际始终加载完整 `sys_prompt.md`。其中约：

- 数据源访问规范 `8k` token；
- 结果保存规范 `7.9k` token；
- Python 代码规范 `10.6k` token；
- 消息上下文与 helper 说明 `13.7k` token；
- 图表契约 `3.2k` token。

关系表分析仍会收到本地文件、Timescale、VxBase 和所有示例；首次分析也会收到大量只在失败后才需要的修复规则。Preparation 已产出 `query_capabilities`，但 Runtime 没有使用它选择 Prompt 模块。

### 3.6 本轮引入：澄清事实持久化存在静默丢失风险

当前澄清答案按 `subject + ambiguity_kind` 合并，并把 `denominator_definition` 归一成 `metric_definition`。同一主题下多个互补定义可能互相覆盖。Runtime 还只保留前 8 项澄清事实，超过后会静默丢失。

澄清事实应该按稳定的 `clarification_item_id` 和版本持久化，不应按自然语言主题做宽泛去重，也不应设置会静默改变业务含义的数量截断。

## 4. 正向改动

以下改动方向正确，应保留并与语义审核链解耦：

1. `request_analysis_clarification` 作为统一 Runtime 的终止型工具；
2. `waiting_clarification` 任务状态、SSE 恢复和同 turn 继续执行；
3. 暂停时保存 Preparation 结果，恢复时不重新选源；
4. 恢复时带回有界、结构化的 Probe 观察；
5. 外层恢复轮次保留原 System/Human/AI/Tool 消息，避免代码修复脱离原 ReAct 上下文；
6. 后置 Evidence 在分析结果提交后独立编译，失败不回滚正式分析；
7. 本轮 Evidence 对目标报告、图表和表格已达到 complete，且没有造成正式分析错误。

## 5. 需要撤回或重做的改动

### 5.1 撤回

1. `runtime_semantic_adherence_service.py` 的 Semantic Decision Compiler；
2. 每次正式代码提交前的 Semantic Adherence Reviewer 硬门槛；
3. `probe_requirement_specs/candidate_fields` 静态观察校验；
4. Probe diagnostics 中重复注入 `authoritative_business_facts`；
5. 把澄清回答编译成总体、粒度、分子分母、公式等指标边界的文档和 Prompt 描述。

### 5.2 重做

1. 澄清答案按 item id/version 保存并原样注入；
2. Probe 只返回技术观察，Runtime 自己把观察与用户业务定义结合；
3. Prompt 从单一大文件拆成稳定核心与按需能力模块；
4. 失败修复说明只在对应错误发生后加载，不进入首次调用；
5. Prompt 预算覆盖完整必选上下文，并记录每个模块的 token 使用；完整 Schema 不裁剪字段，只去重表示和按已选类型加载协议。

## 6. 收敛后的 Runtime 设计

```text
Preparation
  -> 用户问题 + 已选完整 Schema + 已确认 ER/业务血缘（advisory）
  -> 统一 Runtime ReAct
       -> 信息充分：直接 analysis
       -> 技术未知：Probe -> 结构化观察 -> 同一 ReAct
       -> 关键业务歧义：同轮次澄清 -> 原始答案 + 既有 Probe 观察 -> 统一 Runtime
       -> execute_python 失败：确定性诊断 -> 同一 ReAct 修复
       -> 成功分析结果
  -> 后置 Evidence
```

统一 Runtime 只接收一份权威语义块：原用户问题、会话业务规则、Preparation 交接、用户澄清原文和 Probe 观察。不存在中间指标计划，也不存在另一个 LLM 对正式代码做阻断式业务语义裁决。

本地校验只处理可确定证明的边界：

- Python/SQL 语法和安全；
- 字段所属数据源；
- 查询参数与用户显式时间范围；
- API 参数和结构化产物形状；
- 真实执行错误、空数据和容量限制。

业务总体、量词、公式和洞察表达由统一 Runtime 在同一语义上下文中完成。无法唯一决定且会改变答案时，才向用户澄清。

## 7. 落地顺序

### 第一步：恢复单一语义所有权

- 移除语义编译、二次审核和候选字段硬门槛；
- 保留澄清工具、任务挂起恢复和 ReAct 消息连续性；
- 澄清答案改为单一、原样、结构化注入；
- Probe 反馈移除业务事实副本。

验收：含澄清的复跑不再增加编译/审核 LLM 调用；目标轮次能在一次 Probe 后一次正式分析完成。

### 第二步：修正澄清持久化

- 按 `clarification_item_id` 保存和恢复；
- 显式记录 supersedes/version；
- 移除 8 项静默截断；
- 区分复跑沿用与新问题重新判断。

验收：多指标、多定义澄清不会覆盖或丢失，重复恢复不重复注入。

### 第三步：模块化 Runtime Prompt

- 提取稳定的角色、洞察方法、Probe/Analysis 生命周期和产物目标作为核心 Prompt；
- 根据 `query_capabilities` 加载 relational、local-file、timescale、vxbase 模块；
- helper/API 按实际可用能力加载；
- 错误修复模块按 error type 延迟加载；
- 移除首次调用中对消息内部结构和历史修复场景的重复说明。

验收：完整 Schema 不丢字段；首次固定上下文进入明确预算；关系型分析不再携带文件和时序协议；报告表达力不低于基线。

### 第四步：收敛 ReAct 反馈

- 同一错误只保留最新完整失败代码引用、确定性 diagnostics 和最小修复目标；
- 不在 ToolMessage、外层恢复指令和 Probe diagnostics 重复相同事实；
- 外层轮次只作为模型提前结束的兜底，继续保留完整 ReAct transcript。

验收：发生一次错误时，下一次修复成功；上下文不会随重复事实线性膨胀。

### 第五步：独立验证后置 Evidence

- 不把 Evidence 改动与 Runtime 语义改造混在同一验收结论中；
- 分别验证报告、图表、表格覆盖率、计算边界唯一性、可读性和耗时；
- Evidence partial 不回滚正确的正式分析，但必须进入独立缺口矩阵。

## 8. 回归标准

每个会话按 Preparation、Runtime、Evidence 三段记录：

- Preparation：选源和关系完整，常规一次通过；
- Runtime：正常路径无额外语义 LLM，Probe 后最多一次正式代码修复；
- 正确性：关键指标用数据库独立复核，不能只看流程成功；
- 表达力：报告包含数据发现、业务判断、必要对比和基于事实的建议，不是指标说明书；
- Evidence：数值位置覆盖完整，同一计算边界唯一绑定，不误绑日期、序号和展示字面量；
- 性能：相较提交基线不回退，并分别记录 Preparation、首次 Runtime、Probe、修复和 Evidence 耗时。

在第一、二步完成并通过目标轮次前，不继续增加新的语义校验、Prompt 禁止项或会话特例。

## 9. 2026-08-30 阶段性验收记录

226 环境空间 15 的会话 47、轮次 68 在当前代码下完成一次 Probe 和一次正式分析，正式阶段
没有代码修复。总耗时约 `184.63s`，产出 4 个图表、3 个表格和完整洞察报告。独立 FedQuery
复核确认 OOS/近限规模、批次数、Top 产品/检测项、候选占比、最大规格偏离、过程报警和
异常单统计均与报告一致。

该轮最初后置 Evidence 为 partial，原因是嵌套映射读取的本地数据流证明只保存在计算候选，
未同步回 canonical report output，导致两处已证明数值被错误交给 Planner。修复后重新编译为
complete：25 个 EvidenceDefinition、4 个图表 ref、9 个表格 ref、21 个报告 ref，未决位置为 0。
这项修复没有扩大 LLM 输入，也没有调整正式分析 Prompt 或报告表达边界。

## 10. Runtime 上下文能力分层落地

本轮已按 [Runtime 上下文与能力契约设计](runtime-context-capability-design.md) 完成第一版落地：

- `sys_prompt.md` 从约 `84k` 字符收敛为约 `3k` 字符的洞察语义内核；
- 新增显式 Runtime Contract Registry，不再按 Markdown 标题位置切割协议；
- 后端在首次模型调用前一次编译全部 required Pack，关系、时序、文件混合分析取能力并集并增加跨源协议；
- 模型不再调用协议加载工具，避免额外 LLM 回合、ToolMessage 协议权威性不足和加载前执行的中间状态；
- 当前关系型首次 System Prompt 约 `18k` 字符，时序约 `21k` 字符，关系+时序+本地文件混合追问约 `25k` 字符；这些数字包含语义内核和全部必需协议，仍明显低于原约 `84k` 字符固定 Prompt；
- 完整选定 Schema、用户问题、已确认关系和澄清答案仍是常驻权威事实，不按字符预算裁剪；
- 移除 `confirm_analysis_semantics` 额外工具回合，澄清答案由同一个 Runtime 直接消费；
- 正式结果校验不再调用后置 Evidence AST 裁定表格业务指标是否可比，恢复校验与 Evidence 的职责边界；
- 新增能力对齐测试，检查 Runtime 暴露 API 均有协议说明，并固定关系查询、时序原生方言、完整 Schema 解释、追问记忆和洞察产物能力。

该改造解决的是固定 Prompt 膨胀和协议组织问题，不以减少 Runtime 查询、指标或报告能力为代价。后续真实环境回归仍需分别记录首次 Contract 加载耗时、首次代码成功率、修复次数和报告质量。

## 11. 2026-08-30 Runtime 契约稳定性补强

在模块化 Prompt 基础上进一步修正了四个实现错位：

1. 静态校验采用 `valid/invalid/unknown` 三态边界。只有静态代码能够证明口径冲突时才阻断；动态参数交给运行时已求值的 `validate_query_constraints(...)` 验证。
2. 修复范围只由成功或明确无数据的物理 QuerySnapshot 证明。失败代码中的 `used_datasource_ids`、SQL 字面量和上一条拒绝诊断不再自行升级为下一次修复的权威业务范围。
3. 新增 `query_data_with_sql(...)` 组合 API，将常规 SQL 的约束校验、QuerySnapshot 注册和加载固定为一个后端操作；底层 API 继续保留给复杂查询，不缩减查询和洞察能力。
4. 关系型与本地文件能力包保留完整闭环示例，且示例源码直接经过 Runtime 静态契约测试；运行时 Registry 省略这些作者级完整示例，只向模型交付 API 签名、顺序和边界，避免示例中的特定聚合及报告结构主导真实问题。

同时修正了本地文件空 SQL 被 SQL 解析器误处理、明确排序后的第二/第三名仍被极值校验拒绝，以及 `no_data` 物理查询未被范围证明识别的问题。以上修改均属于协议承接和确定性验证边界，不增加语义规划 Stage，也不把报告内容收敛为固定模板。

后续审计进一步把静态发现分为阻断和 advisory：动态 SQL 排序、未知过滤 helper、`mapping.get(..., 0)` 与分组 `first/last` 的业务函数依赖无法仅凭 AST 证明错误，因此不触发修复循环；明确无排序却声称极值、明确单边有限窗口、明确不同筛选总体仍会阻断。风险识别能力保留在日志诊断中，不再把 `unknown` 当成 `invalid`。

本地文件常规路径新增 `query_local_file(...)` 和 `query_local_file_low_memory(...)`，与 SQL 组合 API 使用同一内部 Retry 控制信号，并将校验与物理读取保存在同一个 QuerySnapshot。Runtime 同时记录每个 Prompt Pack、最终上下文、每轮模型输入输出和 ReAct transcript 的大小、token、耗时与结束原因，为真实回归中的超时、截断和上下文线性增长提供确定性证据。
