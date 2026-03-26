from typing import List, Optional, Any, TypeVar, Generic
from dao.base_dao import BaseDAO

T = TypeVar('T', bound=BaseDAO)

class BaseService(Generic[T]):
    """Service基类"""

    def __init__(self, dao: BaseDAO):
        self._dao = dao
    def _get_dao(self) -> T:
        return self._dao
    def find_all(self) -> List[Any]:
        """查询所有"""
        return self._dao.find_all()

    def find_by_id(self, id: int) -> Optional[Any]:
        """根据ID查询"""
        return self._dao.find_by_id(id)

    def save(self, entity: Any) -> Any:
        """保存"""
        return self._dao.save(entity)

    def update(self, entity: Any) -> Optional[Any]:
        """更新"""
        return self._dao.update(entity)

    def delete(self, id: int) -> bool:
        """删除"""
        return self._dao.delete(id)
ST = TypeVar('ST', bound=BaseService)