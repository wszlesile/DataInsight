# DataInsight

DataInsight 是一个面向多轮数据洞察场景的 Agent 项目。系统通过大模型生成并执行 Python 分析代码，基于会话级上下文持续输出图表、分析报告和结构化表格结果，同时保留执行记录、轮次历史和会话记忆，支持后续追问、重跑和结果导出。

## 项目定位

这个项目不是普通问答机器人，而是“代码生成与执行型”的数据分析 Agent。核心目标是：

- 支持在同一会话中持续追问和继续分析
- 把数据源、轮次、执行记录、图表和报告统一持久化
- 让历史分析结果既能回放，也能继续参与下一轮上下文

当前实现同时覆盖：

- 后端：Flask + SQLAlchemy + Agent 编排
- 前端：Vue 3 + Vite + Element Plus
- 模型接入：MiniMax / Qwen

## 核心能力

- 多轮会话分析：同一会话内持续追问、延续分析主线
- 动态数据源绑定：数据源属于空间，会话通过勾选关系绑定使用
- 代码执行留痕：保存生成代码、执行状态、错误信息和结构化结果
- 图表与报告产物：图表、报告、表格按产物持久化
- 原轮次重跑：在同一 `turn` 上重新执行分析，不新增新轮次
- 结果导出：支持单轮分析结果导出为 PDF
- 收藏能力：支持收藏整轮分析结果或单个图表产物

## 当前架构

### 后端分层

- `src/controller`
  - Web 接口入口，负责参数接收和响应组装
- `src/service`
  - 业务编排、上下文持久化、历史查询和导出
- `src/model`
  - SQLAlchemy ORM 模型定义
- `src/agent`
  - Agent 创建、Prompt 组装、运行时流转、工具执行
- `src/utils`
  - JSON、数据源、图表导出等通用工具

### Agent 运行主链路

1. 前端发起分析请求
2. 后端创建或恢复会话，并创建新轮次
3. 根据当前会话绑定关系组装数据源上下文
4. 组装系统提示词、会话记忆、历史问答和当前用户问题
5. 大模型生成 Python 分析代码并调用 `execute_python`
6. 工具执行代码并保存执行记录
7. 成功时写入图表、报告、表格等产物
8. 刷新会话级摘要记忆，供下一轮继续分析

## 核心数据模型

### 空间与资源

- `insight_namespace`
  - 洞察空间
- `insight_datasource`
  - 空间级数据源定义
- `insight_ns_rel_datasource`
  - 会话级数据源绑定关系
- `insight_knowledge`
  - 全局知识资源
- `insight_ns_rel_knowledge`
  - 会话级知识资源绑定关系

### 会话与上下文

- `insight_ns_conversation`
  - 会话主边界，保存当前状态和活动数据源快照
- `insight_ns_turn`
  - 单轮分析事实，保存用户问题、本轮数据源快照和最终回答
- `insight_ns_message`
  - 用于历史回放的用户/助手消息
- `insight_ns_memory`
  - 压缩记忆，包括 `rolling_summary` 和 `analysis_state`

### 执行与产物

- `insight_ns_execution`
  - Python 代码执行记录，包含生成代码、执行状态、结构化结果和错误信息
- `insight_ns_artifact`
  - 单轮派生产物，当前主要包括：
  - `chart`
  - `report`
  - `table`
- `insight_user_collect`
  - 用户收藏

## 上下文工程摘要

当前上下文工程围绕“会话是主上下文边界、轮次是历史事实边界”设计。

进入模型前，后端会按以下顺序组装上下文：

1. 系统提示词
2. 数据源上下文
3. 会话记忆
4. 历史问答消息
5. 当前用户问题

其中会话记忆主要包括：

- `rolling_summary`
  - 会话滚动摘要
- `analysis_state`
  - 当前分析状态，包括最近几轮数据源使用情况、最近执行摘要、最近产物摘要等

更完整的设计说明见：

- [context-engineering-design.md](D:/PycharmProjects/DataInsight/context-engineering-design.md)

## 当前前后端协作方式

### 空间与会话

- 创建空间时会自动创建一条默认会话
- 前端可以在空间下继续新建更多会话
- 当前删除空间时，会同步删除该空间下的会话和上下文数据

### 空间数据源与会话绑定

- 上传文件数据源是空间级动作
- 会话是否使用某个数据源，由会话绑定关系决定
- 前端主列表展示的是“空间下数据源”
- 后端在空间数据源列表接口里直接返回 `checked`，表示当前会话是否已绑定

### 图表与导出

- 对话结果展示基于结构化 `chart_spec`
- PDF 导出由后端根据当前轮产物动态生成
- 图表下载由前端基于当前图表渲染结果导出

## 目录结构

```text
DataInsight/
├─ src/
│  ├─ agent/
│  ├─ config/
│  ├─ controller/
│  ├─ dto/
│  ├─ middleware/
│  ├─ model/
│  ├─ service/
│  └─ utils/
├─ frontend/
├─ src/agent/sys_prompt.md
├─ context-engineering-design.md
├─ backend-api-spec.md
└─ pyproject.toml
```

## 启动方式

### 后端

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe src\bootstrap.py
```

默认地址：

- `http://127.0.0.1:5000`

健康检查：

- `GET /health`

### 前端

在 `frontend` 目录执行：

```powershell
npm install
npm run dev
```

生产构建：

```powershell
npm run build
```

## 配置说明

当前主要配置在：

- [src/.env](D:/PycharmProjects/DataInsight/src/.env)

重点配置项包括：

- `DB_TYPE`
- `SQLITE_PATH`
- `TEMP_DIR`
- `UPLOAD_DIR`
- `LLM_MODEL_ACTIVE`
- `MINIMAX_M2_5_*`
- `QWEN3_80B_*`

## 文档

- 项目上下文工程设计：
  - [context-engineering-design.md](D:/PycharmProjects/DataInsight/context-engineering-design.md)
- 后端接口说明：
  - [backend-api-spec.md](D:/PycharmProjects/DataInsight/backend-api-spec.md)
- Agent 系统提示词：
  - [src/agent/sys_prompt.md](D:/PycharmProjects/DataInsight/src/agent/sys_prompt.md)
