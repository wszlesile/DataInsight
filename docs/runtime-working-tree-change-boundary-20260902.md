# Runtime 待提交改动边界

日期：2026-09-02

基线：`be9d3fd7bbaf81ce354459425a85d41078799e40`

本文用于约束 Runtime 因果对照和后续调整。当前工作区包含产品需求、已证明的工程修复、
仍待验证的 Runtime 实验以及本地环境配置，禁止按文件或整个工作区统一回滚。

## 1. 必须保留的产品能力

### 1.1 同轮次澄清

- 交互调用可按策略启用或禁用澄清。
- 任务进入 `waiting_clarification` 后持久化挂起状态和恢复上下文。
- SSE 可发布并在断线恢复时重放澄清请求。
- 用户提交快捷选项或自由文本后继续原任务、原轮次和原分析语义。
- 前端提供澄清卡片、提交与继续分析交互。

主要代码边界：

- `src/agent/invocation_policy.py`
- `src/agent/invoker.py` 中澄清提取、恢复上下文及调用策略代码
- `src/agent/tools.py` 中澄清输入与 `request_analysis_clarification`
- `src/service/analysis_task_runner.py`
- `src/service/analysis_task_service.py`
- `src/service/conversation_context_service.py`
- `src/controller/agent_controller.py`
- `src/controller/insight_ns_conversation_controller.py`
- `frontend/src/App.vue`、`frontend/src/api/agent.js`
- `docs/runtime-clarification-design.md` 及对应测试

### 1.2 外部业务工作流调用

- 通过隐藏的内部空间和会话提交分析作业，调用方无需创建普通空间、会话。
- 支持 `standard`、`text` 和带 Schema 的 `json` 输出契约。
- `analysis_preferences` 使用文本表达调用方偏好。
- 工作流调用禁用同轮次澄清和后置 Evidence。
- 支持作业创建、状态读取和取消，保留幂等及结果持久化。

主要代码边界：

- `src/controller/workflow_insight_controller.py`
- `src/service/workflow_insight_job_service.py`
- `src/agent/invocation_policy.py`
- `src/model/__init__.py`
- `src/db_migrations/1.8.0.sql`
- `docs/workflow-insight-api-integration.md`
- `docs/workflow-insight-invocation-design.md`
- `docs/backend-api-spec.md` 中相应接口
- 对应 controller、service、output contract 测试

### 1.3 独立保留的既有增量

- 后置 Evidence 的正式分析隔离边界及已经验收的结构化产物绑定修复。
- 普通问答与分析任务分流、任务取消、任务状态隔离等已确认功能。
- 本地执行、洞察任务、认证和数据源服务中已提交基线之上的既定需求，不得被 Runtime
  实验的回退顺带删除。

这些能力若与 Runtime 文件重叠，必须按函数或代码块处理，不能按文件恢复基线。

## 2. 已证明问题的工程修复

下列内容需要保留并独立复验，不能因为业务准确性尚未达标而恢复旧缺陷：

- Probe、正式提交、错误观察和修复在同一 ReAct 历史中连续承接。
- 工具观察不能通过 `return_direct` 提前结束 Agent 图。
- 正式结果保存后由框架终止后续模型请求。
- 查询失败保留真实错误，失败不能冒充空集或 `row_count=0`。
- Probe 观察、无数据事实和失败记忆按当前 task/execution 隔离。
- 合法行级源码修复可被应用；报告坐标不能冒充 Python 源码坐标。
- 补丁拒绝后原版本及坐标不变，错误反馈保留原目标，不能让模型在候选版本行号上继续打补丁。
- Probe 查询失败与空集分开；状态信封不部分模拟 DataFrame，不能把失败隐藏成空表或缺列。
- 明确日历周与未明确的业务“周期”分开，不由文本子串匹配强制规定业务时间范围。
- 输出格式、澄清策略和 Evidence 开关由 `InvocationPolicy` 统一提供。
- 知识参考来源完整保留原有 Schema 规则，不仅传名称和总描述；仍不把参考来源加入物理查询范围。
- 分析刷新提示消费已确定的路由，不根据历史是否执行成功再次许可普通问答分流。
- 准备信息中的空待确认列表不能被本地提示升级成“语义已完整”；原始事实与状态保持不变。
- 字段范围参考与实际 Schema 投影一致，既支持显式列表，也说明 `allowed_fields_ref` 引用。

这些条目解决执行链路的确定性错误，不证明业务算法正确，也不作为新增语义约束。

## 3. 待验证的 Runtime 实验

以下内容必须通过固定输入、首版正式代码和端到端结果的单因子对照后决定去留：

- `sys_prompt.md` 语义内核与 `contracts/` 的拆分方式及实际消息布局。
- `runtime_contract_registry.py` 和 `runtime_prompt_loader.py` 的能力选择与装配。
- 组合查询 API 是否真正减少模型机械工作，是否引入新的声明负担。
- 新增 DataFrame、SQL 投影、查询依赖和查询结果消费静态检查。
- 错误反馈的内容量、定位信息和局部修复策略。
- Prompt 缩短后，首次程序对分析范围、总体、粒度、时间、公式和报告表达的影响。
- 可选 `read_runtime_reference` 对机械错误和调用开销的净影响；必需协议已经常驻，不因该工具增加强制查阅步骤。完整示例、真实签名和输出策略一致性已做工程验证，但不等于模型实际使用收益已证明。

不能用以下代理指标决定这些实验成功：单份 Prompt 变短、单元测试通过、代码成功执行、
Evidence 状态为 complete 或最终报告存在。

成功后 LLM 一致性阻断审核已在真实试验中出现确定误报，相关试验服务、配置与接入已删除。
不能因为准确性尚未验收而重新引入相同的审核重试循环。详见基线审计文档第 12 节。

## 4. 受保护的环境与工作文件

- `src/.env` 是本地回归环境配置，不参与任何源码回退、提交或对照包覆盖。
- `.codex-regression/` 只存放独立对照产物，不回写业务数据库，除非用户明确要求。
- 远端服务、历史分析结果和 Evidence 不因本地因果实验而更新。

## 5. 后续调整规则

1. 基线对照使用独立源码副本；已证实的修复逐项应用到工作区，不能覆盖其他未提交需求。
2. 每个实验只改变一个可描述因子，并记录实际模型消息、首次程序、每次修复和终态。
3. 确认负向影响后，只撤销该实验对应的函数或代码块。
4. 每次调整后复验同轮次澄清、工作流三种输出、工作流禁用 Evidence、任务取消及 Runtime
   同图修复，避免产品需求回退。
5. 业务验收同时检查准确性、报告表达、产物、Evidence 和总耗时，不能以跑通代替通过。
