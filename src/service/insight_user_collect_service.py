from typing import Optional, Dict, Any, List

from dao.insight_user_collect_dao import InsightUserCollectDAO
from model import InsightUserCollect
from service.base_service import BaseService


class InsightUserCollectService(BaseService[InsightUserCollectDAO]):
    """用户收藏业务逻辑层"""

    def __init__(self, dao: InsightUserCollectDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_user_collect_service = self
        super().__init__(dao)

    @property
    def collect_dao(self) -> InsightUserCollectDAO:
        return self._get_dao()

    def find_by_username(self, username: str) -> List[InsightUserCollect]:
        return self.collect_dao.find_by_username(username)

    def add_collect(self, username: str, insight_context_id: int) -> Dict:
        """添加收藏"""
        existing = self.collect_dao.find_by_username_and_context_id(username, insight_context_id)
        if existing:
            return {"success": False, "message": "已收藏"}

        collect = InsightUserCollect(username=username, insight_context_id=insight_context_id)
        saved = self.collect_dao.save(collect)
        return {"success": True, "message": "收藏成功", "data": self._to_dict(saved)}

    def remove_collect(self, username: str, insight_context_id: int) -> Dict:
        """移除收藏"""
        if self.collect_dao.delete_by_username_and_context_id(username, insight_context_id):
            return {"success": True, "message": "取消收藏成功"}
        return {"success": False, "message": "收藏不存在"}

    def is_collected(self, username: str, insight_context_id: int) -> bool:
        """检查是否已收藏"""
        return self.collect_dao.find_by_username_and_context_id(username, insight_context_id) is not None

    def _to_dict(self, collect: InsightUserCollect) -> dict:
        return {
            "id": collect.id,
            "username": collect.username,
            "insight_context_id": collect.insight_context_id,
            "created_at": collect.created_at.isoformat() if collect.created_at else None
        }
