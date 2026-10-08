# 洞察任务架构与运行契约

## 目标

洞察任务将一次成功分析固化为可重复执行的本地分析逻辑，支持立即执行，以及每天、工作日、每周、每月定时运行，并保留结果历史与对比。默认运行不进入 Preparation、正式分析 LLM 或后置 Evidence 编译。

每月任务配置 1 至 31 日；当目标月份不存在该日期时，在当月最后一天执行。立即执行是所有任务共有能力，不作为调度频率。

## 运行边界

1. 任务只能从成功且包含 `generated_code` 的执行记录创建。
2. 任务版本保存不可变的代码、参数协议、数据源绑定和逻辑签名。
3. 每次运行读取最新数据，但不能修改任务来源会话和轮次的当前结果。
4. 本地执行与任务执行共同使用 `analysis_runtime.LocalAnalysisRuntime`。
5. 任务结果独立写入 `insight_task_run`，内部 `insight_ns_execution` 仅作禁用展示的执行审计。
6. JWT、浏览器票据和 Service Account appkey 不写入任务定义、版本或运行历史。
7. 任务版本保存来源执行的查询快照；每次运行保存本次真实查询快照，对外只展示业务口径摘要，不暴露生成代码。
8. 创建任务时，来源会话中的成功分析结果同步投影为一条 `trigger_type=initial` 的成功运行记录。该记录复用来源报告、图表、表格、参数、查询快照和耗时，不重新查询数据，也不调用 Runtime 或 LLM。

任务详情将参数协议中的本次取值与 QuerySnapshot 中的真实查询约束合并为“统计口径”，按时间范围、维度、指标、过滤、参数取值、去重规则、业务定义和口径来源展示。参数 JSON、内部查询键和执行代码不作为用户界面口径。

## 领域模型

- `insight_task_definition`：用户可管理的任务、结构化调度规则和运行状态。
- `insight_task_revision`：不可变执行快照；任务运行始终绑定具体版本。
- `insight_task_run`：初次执行结果，或一次手动、定时运行及其报告、图表、表格、耗时和错误。
- `insight_analysis_task`：继续只表示交互式分析后台任务，不属于洞察任务领域。

## 调度与认证

手动“立即执行”在当前用户请求内创建运行记录并直接调用本地 Runtime，使用当前用户的 Gateway JWT、平台票据和数据权限。用户凭证只存在于请求内存，不进入任务定义、任务版本或运行历史。

`trigger_type=initial` 只表示创建任务时从来源分析固化的初始历史结果，状态在创建时即为 `success`，不进入任务队列、租约恢复或 Runner 调度。

调度器只领取 `trigger_type=scheduled` 的运行记录，通过数据库 `next_run_at` 发现到期任务，以 `(task_definition_id, scheduled_for)` 保证幂等。运行记录采用原子状态抢占和执行租约，进程异常后只恢复过期租约。Gateway 超时、限流和服务端错误采用持久化的有界指数退避；确定性客户端错误直接结束，避免无效重试。

任务 Runner 每次使用 `SUPOS_DATAINSIGHT-SERVER_APPKEY` 经过 Gateway 请求内部执行接口。Gateway 将 appkey 放入 `origin-token`，并将 Service Account JWT 放入 `Authorization`；平台接口继续使用前者，FedQuery 使用后者。内部执行接口必须校验原始 appkey 与服务配置一致。

内部执行接口只向 Runner 返回运行状态和审计标识。完整报告、图表、表格和查询快照直接持久化在任务历史中，不经 Gateway 二次回传。

## 部署角色

- `INSIGHT_APP_ROLE=all`：首版单容器，同时运行 API、洞察任务 Runner 与 Evidence Runner。
- `INSIGHT_APP_ROLE=api`：只运行在线 API。
- `INSIGHT_APP_ROLE=task-runner`：只运行调度、本地执行与 Evidence Runner。

当前保持单一镜像。负载需要隔离时，用同一镜像启动 `api` 和 `task-runner` 两个容器；只有镜像体积本身成为问题时再拆分镜像。

本地通过 `bootstrap.py` 启动时也遵循同一角色语义：`all` 会以守护线程启动两个 Runner，`api` 不启动 Runner，`task-runner` 启动洞察任务 Runner 并以守护线程启动 Evidence Runner。本地洞察任务 Runner 使用 `.env.local` 中的短 Token 与 FedQuery JWT 直接调用同进程执行服务，不绕行已部署环境；生产 Runner 仍通过 Gateway 和 Service Account 派发。若 API 与 Runner 分容器部署，至少一个 `task-runner` 实例必须运行，否则新分析的 Evidence 会保持 `queued`。

## 会话 Evidence 后处理

成功分析先提交报告和产物，再持久化 `insight_ns_execution_evidence.compilation_status=queued` 并立即返回。后台 Evidence Runner 以数据库条件更新抢占任务，将状态设为 `running`，编译后写回 `complete`、`partial` 或 `failed` 与诊断；会话轮次详情同步返回 `evidence_compilation` 状态。详情页面在排队/编译期间轮询轻量状态接口，完成后刷新产物引用。进程中断留下的 `running` 任务在 15 分钟无更新后重新排队。该队列复用既有 Evidence 表，不增加任务表；Evidence 生成规则和覆盖标准不变。

## 运行参数

- `INSIGHT_TASK_SCHEDULER_POLL_SECONDS`：到期任务扫描间隔。
- `INSIGHT_TASK_MAX_CONCURRENCY`：单个 Runner 的最大并发数。
- `INSIGHT_TASK_RUN_LEASE_SECONDS`：运行租约下限。
- `INSIGHT_TASK_DISPATCH_TIMEOUT_SECONDS`：Gateway 内部执行请求超时。
- `INSIGHT_TASK_DISPATCH_MAX_ATTEMPTS`：可重试派发错误的最大尝试次数。
- `INSIGHT_TASK_DISPATCH_RETRY_BASE_SECONDS`：派发重试的基础退避秒数。

## Evidence 与 LLM

任务执行沿用本地重跑契约，不重新规划或调用模型推断 Evidence。新原生程序冻结计算与展示定义，Runtime 在每次实际执行中依据新的 QuerySnapshot、参数和结果生成当前执行的消费回执，Evidence Runner 仅本地投影与持久化。旧程序继续复用既有冻结 Evidence 定义的兼容路径。不得复用父执行的结果摘要、执行 token 或位置绑定；不得要求正式分析代码另写一份 Evidence 字典或重算指标。

默认任务执行不得调用 LLM。阈值触发后的结论增强属于独立的后处理能力，即使增强失败，本地运行结果仍应保持成功和可查看。
