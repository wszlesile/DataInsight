# DataInsight

DataInsight 是一个面向数据洞察场景的多轮分析 Agent 项目。  
系统通过大模型生成 Python 分析代码，执行后输出图表和分析报告，并支持会话级上下文持久化、多轮追问和历史回放。

## 1. 项目定位

这个项目不是普通聊天机器人，而是“代码生成与执行型”数据分析 Agent。

核心链路如下：

1. 用户提出数据分析问题
2. 大模型根据系统提示词生成 Python 分析代码
3. 后端执行代码读取数据源并完成分析
4. 代码产出图表、报告和结果文件
5. 系统保存对话、执行记录、记忆和派生产物
6. 用户基于历史结果继续追问

因此，系统真正的主产物是“生成并执行的 Python 代码”，图表和报告是代码执行后的派生产物。

## 2. 技术栈

### 后端

- Python 3.11+
- Flask
- SQLAlchemy
- LangChain / LangGraph
- Pandas

### 前端

- Vue 3
- Vite
- Element Plus

## 3. 目录结构

```text
DataInsight/
├─ src/                        后端源码
│  ├─ agent/                   Agent 编排、上下文组装、工具执行
│  ├─ config/                  配置、数据库初始化、应用工厂
│  ├─ controller/              Web 控制器
│  ├─ dao/                     数据访问层
│  ├─ model/                   ORM 模型
│  ├─ service/                 业务服务层
│  └─ utils/                   通用工具
├─ frontend/                   前端项目
├─ sys_prompt.md               Agent 系统提示词
├─ context-engineering-design.md  上下文管理设计文档
├─ README.md
└─ pyproject.toml
```

## 4. 后端核心架构

### 4.1 分层结构

后端整体遵循典型 Web 分层：

- `controller`：负责 HTTP 接口接入
- `service`：负责业务编排和上下文管理
- `dao`：负责数据库访问
- `model`：负责 ORM 模型定义

在此基础上，`agent` 模块承担 Agent 相关能力：

- Prompt 组装
- 多轮上下文工程
- 工具调用
- Python 代码执行

### 4.2 核心模块

- `src/agent/__init__.py`
  - 创建模型与 Agent
  - 组织输入消息
- `src/agent/context_engineering.py`
  - 组装数据源上下文
  - 组装历史消息和记忆
- `src/agent/invoker.py`
  - 负责一次 Agent 调用的完整编排
- `src/agent/tools.py`
  - 提供 `execute_python`
  - 承担 Python 代码执行和执行留痕
- `src/service/conversation_context_service.py`
  - 负责会话、轮次、消息、执行、记忆、产物的主链路持久化

## 5. 数据模型概览

当前上下文工程围绕以下核心模型展开：

- `insight_namespace`
  - 洞察空间
- `insight_datasource`
  - 空间内数据源定义
  - `knowledge_tag` 作为数据源级唯一标识标签
  - 当前 `datasource_type` 取值为 `local_file`、`minio_file`、`table`、`api`
- `insight_ns_rel_datasource`
  - 会话与数据源绑定关系
- `insight_ns_conversation`
  - 会话上下文主边界
- `insight_ns_turn`
  - 单轮分析事实
- `insight_ns_message`
  - 对话消息明细
- `insight_ns_execution`
  - 生成并执行的 Python 代码记录
- `insight_ns_artifact`
  - 图表、报告等派生产物
- `insight_ns_memory`
  - 摘要与分析状态
- `insight_user_collect`
  - 收藏能力

## 6. Agent 运行流程

### 6.1 请求进入

前端通过以下接口发起分析：

- `POST /api/agent/invoke`
- `POST /api/agent/stream`

请求体核心字段包括：

- `namespace_id`
- `conversation_id`
- `user_message`
- `datasource_ids`

### 6.2 会话与轮次创建

后端先创建或恢复会话，再创建本轮 `turn`，同时：

- 持久化用户问题
- 处理本轮数据源选择
- 固化轮次级数据源快照

### 6.3 上下文组装

模型调用前，会组装以下上下文：

1. 系统提示词
2. 运行时系统配置
3. 数据源上下文
4. 历史问答消息
5. 记忆上下文
6. 最近代码执行记录
7. 最近派生产物摘要

### 6.4 代码执行

大模型按 `sys_prompt.md` 的约束生成 Python 代码，并通过 `execute_python` 执行。

执行过程中系统会：

- 创建执行记录
- 保存生成代码
- 保存执行状态、stdout、stderr、耗时
- 保存分析报告和结果文件 ID

### 6.5 结果持久化

分析完成后，系统会写入：

- assistant 最终回答
- 执行记录
- 图表与报告等派生产物
- 会话摘要
- 分析状态记忆

## 7. Agent 上下文设计

本项目的上下文管理设计详见：

- [context-engineering-design.md](D:/PycharmProjects/DataInsight/context-engineering-design.md)

这里给出简版说明。

### 7.1 上下文边界

- `namespace_id`：资源归属边界
- `conversation_id`：上下文边界
- `turn`：历史事实边界

### 7.2 为什么能支持多轮持续分析

系统不是简单保存聊天记录，而是分层保存：

- 会话级当前状态
- 轮次级历史事实
- 消息级问答记录
- 执行级代码与执行结果
- 产物级图表与报告
- 记忆级摘要与分析状态

因此下一轮追问时，模型不仅能看到“上轮说了什么”，还能看到：

- 上轮用了哪些数据源
- 上轮生成了什么 Python 代码
- 上轮执行结果是什么

### 7.3 数据源状态如何保存

由于用户每轮都可能重新选择数据源范围，系统同时保存两类状态：

#### 会话级当前状态

保存位置：

- `insight_ns_conversation.active_datasource_snapshot`

含义：

- 当前会话最新激活的数据源范围

#### 轮次级历史事实

保存位置：

- `insight_ns_turn.selected_datasource_ids_json`
- `insight_ns_turn.selected_datasource_snapshot_json`

含义：

- 本轮分析开始时，实际选中的数据源范围

### 7.4 历史上下文如何回注

下一轮组装 Prompt 时，后端会读取：

- 最近历史问答
- `rolling_summary`
- `analysis_state`
- 最近执行摘要
- 最近一次 Python 分析代码
- 最近派生产物摘要

这样模型能在“延续分析逻辑”而不是“重新开始”。

## 8. 接口概览

### Agent 运行接口

- `POST /api/agent/invoke`
- `POST /api/agent/stream`

### 会话相关接口

- `GET /api/insight/conversations`
- `PUT /api/insight/conversations/<conversation_id>`
- `GET /api/insight/conversations/<conversation_id>/history`
- `GET /api/insight/conversations/<conversation_id>/turns/<turn_id>`

### 收藏相关接口

- `GET /api/insight/collects`
- `POST /api/insight/collects`
- `DELETE /api/insight/collects`

## 9. 启动方式

### 9.1 后端

建议先创建并激活虚拟环境，然后在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe src\bootstrap.py
```

默认启动地址：

- `http://127.0.0.1:5000`

健康检查：

- `GET /health`

### 9.2 前端

在 `frontend` 目录下执行：

```powershell
npm install
npm run dev
```

生产构建：

```powershell
npm run build
```

## 10. 当前设计特点

### 已具备

- 多轮对话持久化
- 会话、轮次、消息分层建模
- 动态数据源范围切换
- Python 代码执行留痕
- 图表和报告派生产物保存
- 会话历史回放
- 收藏能力

### 当前保留边界

- 知识资源建模已保留，但尚未正式接入本期 Agent Prompt
- 当前外部工具契约仍为 `StructuredResult(file_id, analysis_report)`
- 当前业务下空间与会话近似 `1:1`，但模型已为未来 `1:N` 扩展预留

## 11. 补充说明

如果需要了解更细的上下文工程设计，包括：

- 多轮会话持久化链路
- 历史上下文如何组装
- 数据源快照如何分层保存
- 代码执行记录如何参与下一轮推理

请直接阅读：

- [context-engineering-design.md](D:/PycharmProjects/DataInsight/context-engineering-design.md)
