from typing import Optional, Dict, Any, List

from dao.insight_namespace_dao import InsightNamespaceDAO
from model import InsightNamespace
from service.base_service import BaseService


class InsightNamespaceService(BaseService[InsightNamespaceDAO]):
    """洞察空间业务逻辑层"""

    def __init__(self, insight_namespace_dao: InsightNamespaceDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_namespace_service = self
        super().__init__(insight_namespace_dao)

    @property
    def insight_namespace_dao(self) -> InsightNamespaceDAO:
        return self._get_dao()

    def find_by_username(self, username: str) -> List[InsightNamespace]:
        return self.insight_namespace_dao.find_by_username(username)

    def find_by_username_and_name(self, username: str, name: str) -> Optional[InsightNamespace]:
        return self.insight_namespace_dao.find_by_username_and_name(username, name)

    def create_namespace(self, username: str, name: str) -> Dict:
        """创建洞察空间"""
        if self.find_by_username_and_name(username, name):
            return {"success": False, "message": "洞察空间名称已存在"}

        namespace = InsightNamespace(username=username, name=name)
        saved = self.insight_namespace_dao.save(namespace)
        return {"success": True, "message": "创建成功", "data": self._to_dict(saved)}

    def update_namespace(self, id: int, name: str) -> Dict:
        """更新洞察空间"""
        namespace = self.find_by_id(id)
        if not namespace:
            return {"success": False, "message": "洞察空间不存在"}

        namespace.name = name
        updated = self.insight_namespace_dao.update(namespace)
        return {"success": True, "message": "更新成功", "data": self._to_dict(updated)}

    def delete_namespace(self, id: int) -> Dict:
        """删除洞察空间"""
        if self.delete(id):
            return {"success": True, "message": "删除成功"}
        return {"success": False, "message": "洞察空间不存在"}

    def _to_dict(self, namespace: InsightNamespace) -> dict:
        return {
            "id": namespace.id,
            "username": namespace.username,
            "name": namespace.name,
            "created_at": namespace.created_at.isoformat() if namespace.created_at else None
        }
