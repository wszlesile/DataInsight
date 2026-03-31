from datetime import datetime
from typing import TypeVar

from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql.sqltypes import Text, Enum

from config.database import Base

POT = TypeVar('POT', bound=Base)


# 洞察空间
class InsightNamespace(Base):
    __tablename__ = 'insight_namespace'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(20), nullable=False, comment='用户名')
    name = Column(String(20), nullable=False, comment='洞察空间名')
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# 洞察空间知识库
class InsightNsRelKnowledge(Base):
    __tablename__ = 'insight_ns_rel_knowledge'
    id = Column(Integer, primary_key=True, autoincrement=True)
    insight_namespace_id = Column(Integer, nullable=False, comment='洞察空间名ID')
    knowledge_name = Column(String(20), nullable=False, comment='知识库名称')
    knowledge_tag = Column(String(20), nullable=False, comment='知识库tag')
    file_id = Column(String(20), nullable=False, comment='文件ID')
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "knowledge_name": self.knowledge_name,
            "knowledge_tag": self.knowledge_tag,
            "file_id": self.file_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# 洞察空间数据源
class InsightNsRelDatasource(Base):
    __tablename__ = 'insight_ns_rel_datasource'
    id = Column(Integer, primary_key=True, autoincrement=True)
    insight_namespace_id = Column(Integer, nullable=False, comment='洞察空间名ID')
    datasource_type = Column(Integer, nullable=False, comment='数据源类型 0-uns 1-文件')
    datasource_name = Column(String(20), nullable=False, comment='数据源名称')
    datasource_schema = Column(Text, nullable=False, comment='数据源JSON Schema')
    knowledge_tag = Column(String(20), nullable=False, comment='知识库tag')
    uns_node_alias = Column(String(20), nullable=False, comment='uns选择节点别名')
    file_type = Column(Integer, nullable=False, comment='文件类型 0-excel 1-csv')
    file_id = Column(String(20), nullable=False, comment='文件ID')
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "insight_namespace_id": self.insight_namespace_id,
            "datasource_type": self.datasource_type,
            "datasource_name": self.datasource_name,
            "datasource_schema": self.datasource_schema,
            "knowledge_tag": self.knowledge_tag,
            "uns_node_alias": self.uns_node_alias,
            "file_type": self.file_type,
            "file_id": self.file_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# 会话
class InsightNsConversation(Base):
    __tablename__ = 'insight_ns_conversation'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(20), nullable=False, comment='用户名')
    insight_namespace_id = Column(Integer, nullable=False, comment='洞察空间名ID')
    user_message = Column(Text, nullable=False, comment='用户输入')
    insight_result = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "insight_namespace_id": self.insight_namespace_id,
            "user_message": self.user_message,
            "insight_result": self.insight_result,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# 会话上下文
class InsightNsContext(Base):
    __tablename__ = 'insight_ns_context'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(20), nullable=False, comment='用户名')
    insight_namespace_id = Column(Integer, nullable=False, comment='洞察空间名ID')
    insight_conversation_id = Column(Integer, nullable=False, comment='会话ID')
    type = Column(Integer, nullable=False, comment='上下文类型 0-系统消息 1-用户消息 2-AI消息 3-工具消息')
    content = Column(Text, nullable=False, comment='上下文内容')
    insight_result = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "insight_namespace_id": self.insight_namespace_id,
            "insight_conversation_id": self.insight_conversation_id,
            "type": self.type,
            "content": self.content,
            "insight_result": self.insight_result,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# 知识库
class InsightKnowledge(Base):
    __tablename__ = 'insight_knowledge'
    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_name = Column(String(20), nullable=False, comment='知识库名称')
    file_id = Column(String(20), nullable=False, comment='知识库文件ID')
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "knowledge_name": self.knowledge_name,
            "file_id": self.file_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# 我的收藏(洞察结果\上下文)
class InsightUserCollect(Base):
    __tablename__ = 'insight_user_collect'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(20), nullable=False, comment='用户名')
    insight_context_id = Column(Integer, nullable=False, comment='上下文记录ID')
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "insight_context_id": self.insight_context_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
