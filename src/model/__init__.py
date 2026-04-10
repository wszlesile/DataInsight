import json
from datetime import datetime
from typing import TypeVar

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from config.database import Base

POT = TypeVar('POT', bound=Base)


def _now() -> datetime:
    return datetime.now()


def _safe_json_loads(value, fallback):
    if not value:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _format(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class InsightNamespace(Base):
    __tablename__ = 'insight_namespace'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='洞察空间主键')
    username = Column(String(64), nullable=False, comment='空间所属用户名')
    name = Column(String(128), nullable=False, comment='洞察空间名称')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_insight_namespace_user_name', 'username', 'name'),
        {'comment': '洞察空间表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
        }


class InsightKnowledge(Base):
    __tablename__ = 'insight_knowledge'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='知识资源主键')
    knowledge_name = Column(String(128), nullable=False, comment='知识资源名称')
    knowledge_tag = Column(String(128), nullable=False, default='', comment='知识资源唯一标识标签')
    file_id = Column(String(255), nullable=False, comment='知识文件 ID')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_insight_knowledge_file', 'file_id'),
        {'comment': '全局知识资源表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "knowledge_name": self.knowledge_name,
            "knowledge_tag": self.knowledge_tag,
            "file_id": self.file_id,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
        }


class InsightNsRelKnowledge(Base):
    __tablename__ = 'insight_ns_rel_knowledge'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='会话知识资源关系主键')
    insight_namespace_id = Column(Integer, nullable=False, comment='所属洞察空间 ID')
    insight_conversation_id = Column(Integer, nullable=False, default=0, comment='所属会话 ID')
    knowledge_id = Column(Integer, nullable=False, default=0, comment='关联全局知识资源 ID')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_rel_knowledge_conversation_knowledge', 'insight_conversation_id', 'knowledge_id'),
        {'comment': '洞察会话与全局知识资源关系表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "insight_conversation_id": self.insight_conversation_id,
            "knowledge_id": self.knowledge_id,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
        }


class InsightDatasource(Base):
    __tablename__ = 'insight_datasource'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='数据源主键')
    insight_namespace_id = Column(Integer, nullable=False, default=0, comment='所属洞察空间 ID')
    datasource_type = Column(String(32), nullable=False, comment='数据源类型')
    datasource_name = Column(String(128), nullable=False, comment='数据源名称')
    knowledge_tag = Column(String(128), nullable=False, default='', comment='数据源唯一标识标签')
    datasource_schema = Column(Text, nullable=False, default='', comment='数据源元数据 Schema')
    datasource_config_json = Column(Text, nullable=False, default='{}', comment='数据源配置 JSON')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')
    updated_at = Column(DateTime, default=_now, onupdate=_now, comment='更新时间')

    __table_args__ = (
        Index('idx_insight_datasource_namespace_name', 'insight_namespace_id', 'datasource_name'),
        Index('idx_insight_datasource_namespace_type', 'insight_namespace_id', 'datasource_type'),
        {'comment': '空间隔离的数据源定义表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "datasource_type": self.datasource_type,
            "datasource_name": self.datasource_name,
            "knowledge_tag": self.knowledge_tag,
            "datasource_schema": self.datasource_schema,
            "datasource_config_json": self.datasource_config_json,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
            "updated_at": _format(self.updated_at),
        }


class InsightNsRelDatasource(Base):
    __tablename__ = 'insight_ns_rel_datasource'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='会话数据源关系主键')
    insight_namespace_id = Column(Integer, nullable=False, comment='所属洞察空间 ID')
    insight_conversation_id = Column(Integer, nullable=False, default=0, comment='所属会话 ID')
    datasource_id = Column(Integer, nullable=False, default=0, comment='关联空间数据源 ID')
    is_active = Column(Integer, nullable=False, default=1, comment='保留字段，不参与当前业务判断')
    sort_no = Column(Integer, nullable=False, default=0, comment='排序号')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')
    updated_at = Column(DateTime, default=_now, onupdate=_now, comment='更新时间')

    __table_args__ = (
        Index('idx_ns_rel_datasource_conversation_datasource', 'insight_conversation_id', 'datasource_id'),
        {'comment': '洞察会话与空间数据源关系表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "insight_conversation_id": self.insight_conversation_id,
            "datasource_id": self.datasource_id,
            "sort_no": self.sort_no,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
            "updated_at": _format(self.updated_at),
        }


class InsightNsConversation(Base):
    """多轮分析会话的上下文主边界。"""

    __tablename__ = 'insight_ns_conversation'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='会话主键')
    insight_namespace_id = Column(Integer, nullable=False, comment='所属洞察空间 ID')
    title = Column(String(255), nullable=False, default='新建洞察', comment='会话标题')
    status = Column(String(32), nullable=False, default='active', comment='会话状态')
    summary_text = Column(Text, nullable=False, default='', comment='会话滚动摘要')
    active_datasource_snapshot = Column(Text, nullable=False, default='{}', comment='当前激活数据源快照 JSON')
    last_turn_no = Column(Integer, nullable=False, default=0, comment='当前会话最新轮次号')
    last_message_at = Column(DateTime, default=_now, comment='最后消息时间')
    user_message = Column(Text, nullable=False, default='', comment='最近一次用户输入，兼容旧逻辑')
    insight_result = Column(Text, nullable=False, default='', comment='最近一次分析结果，兼容旧逻辑')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')
    updated_at = Column(DateTime, default=_now, onupdate=_now, comment='更新时间')

    __table_args__ = (
        Index('idx_conversation_namespace_status', 'insight_namespace_id', 'status'),
        {'comment': '洞察分析会话表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "title": self.title,
            "status": self.status,
            "summary_text": self.summary_text,
            "active_datasource_snapshot": self.active_datasource_snapshot,
            "last_turn_no": self.last_turn_no,
            "is_deleted": self.is_deleted,
            "last_message_at": _format(self.last_message_at),
            "created_at": _format(self.created_at),
            "updated_at": _format(self.updated_at),
        }


class InsightNsTurn(Base):
    """会话中的单轮分析事实。"""

    __tablename__ = 'insight_ns_turn'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='轮次主键')
    conversation_id = Column(Integer, nullable=False, comment='所属会话 ID')
    turn_no = Column(Integer, nullable=False, comment='会话内轮次序号')
    user_query = Column(Text, nullable=False, comment='本轮用户问题')
    selected_datasource_ids_json = Column(Text, nullable=False, default='[]', comment='本轮选中的数据源 ID 列表 JSON')
    selected_datasource_snapshot_json = Column(Text, nullable=False, default='[]', comment='本轮数据源快照 JSON')
    final_answer = Column(Text, nullable=False, default='', comment='本轮最终回答')
    status = Column(String(32), nullable=False, default='running', comment='轮次状态')
    error_message = Column(Text, nullable=False, default='', comment='轮次错误信息')
    started_at = Column(DateTime, default=_now, comment='轮次开始时间')
    finished_at = Column(DateTime, nullable=True, comment='轮次结束时间')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_turn_conversation_turn', 'conversation_id', 'turn_no', unique=True),
        {'comment': '会话轮次表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "turn_no": self.turn_no,
            "user_query": self.user_query,
            "selected_datasource_ids_json": self.selected_datasource_ids_json,
            "selected_datasource_snapshot_json": self.selected_datasource_snapshot_json,
            "selected_datasource_ids": _safe_json_loads(self.selected_datasource_ids_json, []),
            "selected_datasource_snapshot": _safe_json_loads(self.selected_datasource_snapshot_json, []),
            "final_answer": self.final_answer,
            "status": self.status,
            "error_message": self.error_message,
            "is_deleted": self.is_deleted,
            "started_at": _format(self.started_at),
            "finished_at": _format(self.finished_at),
            "created_at": _format(self.created_at),
        }


class InsightNsExecution(Base):
    """一条由大模型生成并执行的 Python 分析记录。"""

    __tablename__ = 'insight_ns_execution'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='代码执行主键')
    conversation_id = Column(Integer, nullable=False, comment='所属会话 ID')
    turn_id = Column(Integer, nullable=False, comment='所属轮次 ID')
    tool_call_id = Column(String(128), nullable=False, default='', comment='工具调用 ID')
    title = Column(String(255), nullable=False, default='', comment='执行任务标题')
    description = Column(Text, nullable=False, default='', comment='执行任务描述')
    generated_code = Column(Text, nullable=False, default='', comment='大模型生成的 Python 代码')
    execution_status = Column(String(32), nullable=False, default='running', comment='执行状态')
    analysis_report = Column(Text, nullable=False, default='', comment='分析报告内容')
    result_payload_json = Column(Text, nullable=False, default='{}', comment='结构化执行结果 JSON')
    stdout_text = Column(Text, nullable=False, default='', comment='标准输出文本')
    stderr_text = Column(Text, nullable=False, default='', comment='标准错误文本')
    execution_seconds = Column(Integer, nullable=False, default=0, comment='执行耗时毫秒数')
    error_message = Column(Text, nullable=False, default='', comment='执行错误信息')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')
    updated_at = Column(DateTime, default=_now, onupdate=_now, comment='更新时间')
    finished_at = Column(DateTime, nullable=True, comment='执行结束时间')

    __table_args__ = (
        Index('idx_execution_conversation_turn', 'conversation_id', 'turn_id'),
        Index('idx_execution_turn_created', 'turn_id', 'created_at'),
        {'comment': '会话代码执行记录表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "title": self.title,
            "description": self.description,
            "generated_code": self.generated_code,
            "execution_status": self.execution_status,
            "analysis_report": self.analysis_report,
            "result_payload_json": self.result_payload_json,
            "stdout_text": self.stdout_text,
            "stderr_text": self.stderr_text,
            "execution_seconds": self.execution_seconds,
            "error_message": self.error_message,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
            "updated_at": _format(self.updated_at),
            "finished_at": _format(self.finished_at),
        }


class InsightNsMessage(Base):
    """用于多轮上下文重放的消息表。"""

    __tablename__ = 'insight_ns_message'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='消息主键')
    insight_namespace_id = Column(Integer, nullable=False, comment='所属洞察空间 ID')
    insight_conversation_id = Column(Integer, nullable=False, comment='所属会话 ID')
    turn_id = Column(Integer, nullable=False, default=0, comment='所属轮次 ID')
    turn_no = Column(Integer, nullable=False, default=0, comment='所属轮次序号')
    seq_no = Column(Integer, nullable=False, default=0, comment='轮次内消息序号')
    role = Column(String(32), nullable=False, default='assistant', comment='消息角色')
    message_kind = Column(String(32), nullable=False, default='final_answer', comment='消息类型')
    type = Column(Integer, nullable=False, default=0, comment='兼容旧版的消息类型编码')
    content = Column(Text, nullable=False, default='', comment='消息正文')
    content_json = Column(Text, nullable=False, default='', comment='结构化消息内容 JSON')
    tool_name = Column(String(128), nullable=False, default='', comment='工具名称')
    tool_call_id = Column(String(128), nullable=False, default='', comment='工具调用 ID')
    insight_result = Column(Text, nullable=False, default='', comment='兼容旧逻辑的结果字段')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_message_conversation_turn_seq', 'insight_conversation_id', 'turn_no', 'seq_no', unique=True),
        Index('idx_message_conversation_created', 'insight_conversation_id', 'created_at'),
        {'comment': '会话消息表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "insight_conversation_id": self.insight_conversation_id,
            "turn_id": self.turn_id,
            "turn_no": self.turn_no,
            "seq_no": self.seq_no,
            "role": self.role,
            "message_kind": self.message_kind,
            "type": self.type,
            "content": self.content,
            "content_json": self.content_json,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "insight_result": self.insight_result,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
        }


class InsightNsMemory(Base):
    """用于压缩上下文和延续状态的会话记忆表。"""

    __tablename__ = 'insight_ns_memory'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='记忆主键')
    conversation_id = Column(Integer, nullable=False, comment='所属会话 ID')
    memory_type = Column(String(32), nullable=False, comment='记忆类型')
    content_json = Column(Text, nullable=False, default='{}', comment='记忆内容 JSON')
    version = Column(Integer, nullable=False, default=1, comment='记忆版本号')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')
    updated_at = Column(DateTime, default=_now, onupdate=_now, comment='更新时间')

    __table_args__ = (
        Index('idx_memory_conversation_type', 'conversation_id', 'memory_type', unique=True),
        {'comment': '会话记忆表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "memory_type": self.memory_type,
            "content_json": self.content_json,
            "version": self.version,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
            "updated_at": _format(self.updated_at),
        }


class InsightNsArtifact(Base):
    """代码执行派生出的会话级分析产物表。"""

    __tablename__ = 'insight_ns_artifact'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='分析产物主键')
    conversation_id = Column(Integer, nullable=False, comment='所属会话 ID')
    turn_id = Column(Integer, nullable=False, comment='所属轮次 ID')
    execution_id = Column(Integer, nullable=False, default=0, comment='所属代码执行 ID')
    artifact_type = Column(String(32), nullable=False, comment='产物类型')
    title = Column(String(255), nullable=False, default='', comment='产物标题')
    summary_text = Column(Text, nullable=False, default='', comment='产物摘要')
    content_json = Column(Text, nullable=False, default='{}', comment='产物核心内容 JSON')
    metadata_json = Column(Text, nullable=False, default='{}', comment='产物元数据 JSON')
    sort_no = Column(Integer, nullable=False, default=0, comment='产物展示顺序')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_artifact_conversation_turn', 'conversation_id', 'turn_id'),
        {'comment': '分析产物表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "execution_id": self.execution_id,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "summary_text": self.summary_text,
            "content_json": self.content_json,
            "metadata_json": self.metadata_json,
            "sort_no": self.sort_no,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
        }


class InsightUserCollect(Base):
    __tablename__ = 'insight_user_collect'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='收藏主键')
    username = Column(String(64), nullable=False, comment='收藏所属用户名')
    collect_type = Column(String(32), nullable=False, default='message', comment='收藏对象类型')
    target_id = Column(Integer, nullable=False, default=0, comment='收藏目标 ID')
    title = Column(String(255), nullable=False, default='', comment='收藏标题')
    summary_text = Column(Text, nullable=False, default='', comment='收藏摘要')
    insight_namespace_id = Column(Integer, nullable=False, default=0, comment='所属洞察空间 ID')
    insight_conversation_id = Column(Integer, nullable=False, default=0, comment='关联会话 ID')
    insight_message_id = Column(Integer, nullable=False, default=0, comment='关联消息 ID')
    insight_artifact_id = Column(Integer, nullable=False, default=0, comment='关联产物 ID')
    metadata_json = Column(Text, nullable=False, default='{}', comment='附加元数据 JSON')
    is_deleted = Column(Integer, nullable=False, default=0, comment='软删除标记')
    created_at = Column(DateTime, default=_now, comment='创建时间')

    __table_args__ = (
        Index('idx_collect_user_type_target', 'username', 'collect_type', 'target_id'),
        {'comment': '用户收藏表'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "collect_type": self.collect_type,
            "target_id": self.target_id,
            "title": self.title,
            "summary_text": self.summary_text,
            "insight_namespace_id": self.insight_namespace_id,
            "insight_conversation_id": self.insight_conversation_id,
            "insight_message_id": self.insight_message_id,
            "insight_context_id": self.insight_message_id,
            "insight_artifact_id": self.insight_artifact_id,
            "metadata_json": self.metadata_json,
            "is_deleted": self.is_deleted,
            "created_at": _format(self.created_at),
        }
