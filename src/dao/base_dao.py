from typing import Generic, Optional, TypeVar

from sqlalchemy.orm import Session

from model import POT


class BaseDAO(Generic[POT]):
    """Base DAO built on top of SQLAlchemy session operations."""

    def __init__(self, model: type[POT], session: Session):
        self._model = model
        self._session = session

    def _base_query(self):
        query = self._session.query(self._model)
        if hasattr(self._model, 'is_deleted'):
            query = query.filter(self._model.is_deleted == 0)
        return query

    def find_all(self) -> list[POT]:
        """Return all non-deleted rows for the current model."""
        return self._base_query().all()

    def find_by_id(self, id: int) -> Optional[POT]:
        """Return one non-deleted row by primary key."""
        return self._base_query().filter(self._model.id == id).first()

    def save(self, entity: POT) -> POT:
        """Persist a new entity and refresh it from the database."""
        self._session.add(entity)
        self._session.commit()
        self._session.refresh(entity)
        return entity

    def update(self, entity: POT) -> Optional[POT]:
        """Commit in-place updates for an existing entity."""
        self._session.commit()
        self._session.refresh(entity)
        return entity

    def delete(self, id: int) -> bool:
        """Delete an entity, preferring soft delete when supported."""
        entity = self.find_by_id(id)
        if entity is None:
            return False

        if hasattr(entity, 'is_deleted'):
            entity.is_deleted = 1
        else:
            self._session.delete(entity)

        self._session.commit()
        return True


DAOT = TypeVar('DAOT', bound=BaseDAO)
