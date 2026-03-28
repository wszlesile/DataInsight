from typing import Optional, Dict, Any, List

from dao.insight_ns_rel_datasource_dao import InsightNsRelDatasourceDAO
from model import InsightNsRelDatasource
from service.base_service import BaseService


class InsightNsRelDatasourceService(BaseService[InsightNsRelDatasourceDAO]):
    """洞察空间数据源关联业务逻辑层"""

    def __init__(self, dao: InsightNsRelDatasourceDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_ns_rel_datasource_service = self
        super().__init__(dao)

    @property
    def rel_datasource_dao(self) -> InsightNsRelDatasourceDAO:
        return self._get_dao()

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsRelDatasource]:
        return self.rel_datasource_dao.find_by_namespace_id(insight_namespace_id)

    def add_datasource(self, insight_namespace_id: int, datasource_type: int, datasource_name: str,
                       knowledge_tag: str, uns_node_alias: str, file_type: int, file_id: str) -> Dict:
        """添加数据源到洞察空间"""
        existing = self.rel_datasource_dao.find_by_namespace_id_and_name(insight_namespace_id, datasource_name)
        if existing:
            return {"success": False, "message": "该数据源名称已存在"}

        rel = InsightNsRelDatasource(
            insight_namespace_id=insight_namespace_id,
            datasource_type=datasource_type,
            datasource_name=datasource_name,
            datasource_schema='',
            knowledge_tag=knowledge_tag,
            uns_node_alias=uns_node_alias,
            file_type=file_type,
            file_id=file_id
        )
        saved = self.rel_datasource_dao.save(rel)
        return {"success": True, "message": "添加成功", "data": self._to_dict(saved)}

    def remove_datasource(self, insight_namespace_id: int, datasource_name: str) -> Dict:
        """从洞察空间移除数据源"""
        rel = self.rel_datasource_dao.find_by_namespace_id_and_name(insight_namespace_id, datasource_name)
        if not rel:
            return {"success": False, "message": "数据源关联不存在"}

        self.rel_datasource_dao.delete(rel.id)
        return {"success": True, "message": "移除成功"}

    def _to_dict(self, rel: InsightNsRelDatasource) -> dict:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "datasource_type": rel.datasource_type,
            "datasource_name": rel.datasource_name,
            "datasource_schema": self.datasource_schema,
            "knowledge_tag": rel.knowledge_tag,
            "uns_node_alias": rel.uns_node_alias,
            "file_type": rel.file_type,
            "file_id": rel.file_id,
            "created_at": rel.created_at.isoformat() if rel.created_at else None
        }
