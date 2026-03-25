from typing import List, Optional, TypeVar, Generic
from sqlalchemy.orm import Session
from config.database import Base

POT = TypeVar('POT', bound=Base)


class BaseDAO(Generic[POT]):
    """DAO基类，基于SQLAlchemy"""

    def __init__(self, model: type[POT], session: Session):
        self._model = model
        self._session = session

    def find_all(self) -> List[POT]:
        """查询所有记录"""
        return self._session.query(self._model).all()

    def find_by_id(self, id: int) -> Optional[POT]:
        """根据ID查询记录"""
        return self._session.query(self._model).filter(self._model.id == id).first()

    def save(self, entity: POT) -> POT:
        """保存记录"""
        self._session.add(entity)
        self._session.commit()
        self._session.refresh(entity)
        return entity

    def update(self, entity: POT) -> Optional[POT]:
        """更新记录"""
        self._session.commit()
        self._session.refresh(entity)
        return entity

    def delete(self, id: int) -> bool:
        """删除记录"""
        entity = self.find_by_id(id)
        if entity:
            self._session.delete(entity)
            self._session.commit()
            return True
        return False
