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

任务详情将参数协议中的本次取值与 QuerySnapshot 中的真实查询约束合并为“统计口径”，按时间范围、维度、指标、过滤、参数取值、去重规则、业务定义和口径来源展示。参数 JSON、内部查询键和执行代码不作为用户界面口径。

## 领域模型

- `insight_task_definition`：用户可管理的任务、结构化调度规则和运行状态。
- `insight_task_revision`：不可变执行快照；任务运行始终绑定具体版本。
- `insight_task_run`：一次手动或定时运行及其报告、图表、表格、耗时和错误。
- `insight_analysis_task`：继续只表示交互式分析后台任务，不属于洞察任务领域。

## 调度与认证

手动“立即执行”在当前用户请求内创建运行记录并直接调用本地 Runtime，使用当前用户的 Gateway JWT、平台票据和数据权限。用户凭证只存在于请求内存，不进入任务定义、任务版本或运行历史。

调度器只领取 `trigger_type=scheduled` 的运行记录，通过数据库 `next_run_at` 发现到期任务，以 `(task_definition_id, scheduled_for)` 保证幂等。运行记录采用原子状态抢占和执行租约，进程异常后只恢复过期租约。Gateway 超时、限流和服务端错误采用持久化的有界指数退避；确定性客户端错误直接结束，避免无效重试。

任务 Runner 每次使用 `SUPOS_DATAINSIGHT-SERVER_APPKEY` 经过 Gateway 请求内部执行接口。Gateway 将 appkey 放入 `origin-token`，并将 Service Account JWT 放入 `Authorization`；平台接口继续使用前者，FedQuery 使用后者。内部执行接口必须校验原始 appkey 与服务配置一致。

内部执行接口只向 Runner 返回运行状态和审计标识。完整报告、图表、表格和查询快照直接持久化在任务历史中，不经 Gateway 二次回传。

## 部署角色

- `INSIGHT_APP_ROLE=all`：首版单容器，同时运行 API 与唯一任务 Runner 进程。
- `INSIGHT_APP_ROLE=api`：只运行在线 API。
- `INSIGHT_APP_ROLE=task-runner`：只运行调度和本地执行。

当前保持单一镜像。负载需要隔离时，用同一镜像启动 `api` 和 `task-runner` 两个容器；只有镜像体积本身成为问题时再拆分镜像。

本地通过 `bootstrap.py` 启动时也遵循同一角色语义：`all` 会以守护线程启动 Runner，`api` 不启动 Runner，`task-runner` 只运行 Runner。本地 Runner 使用 `.env.local` 中的短 Token 与 FedQuery JWT 直接调用同进程执行服务，不绕行已部署环境；生产 Runner 仍通过 Gateway 和 Service Account 派发。

## 运行参数

- `INSIGHT_TASK_SCHEDULER_POLL_SECONDS`：到期任务扫描间隔。
- `INSIGHT_TASK_MAX_CONCURRENCY`：单个 Runner 的最大并发数。
- `INSIGHT_TASK_RUN_LEASE_SECONDS`：运行租约下限。
- `INSIGHT_TASK_DISPATCH_TIMEOUT_SECONDS`：Gateway 内部执行请求超时。
- `INSIGHT_TASK_DISPATCH_MAX_ATTEMPTS`：可重试派发错误的最大尝试次数。
- `INSIGHT_TASK_DISPATCH_RETRY_BASE_SECONDS`：派发重试的基础退避秒数。

## Evidence 与 LLM

首版任务执行沿用本地重跑契约，不重新规划或编译 Evidence。未来若任务结果需要可点击 Evidence，应复用冻结的 Evidence 定义并基于新 QuerySnapshot 本地物化，不把 Evidence 职责重新放回正式分析代码。

默认任务执行不得调用 LLM。阈值触发后的结论增强属于独立的后处理能力，即使增强失败，本地运行结果仍应保持成功和可查看。
