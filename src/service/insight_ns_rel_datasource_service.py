from pathlib import Path
from uuid import uuid4
from typing import Any

import pandas as pd
from werkzeug.datastructures import FileStorage

from sqlalchemy.orm import Session

from dto import DataSourceSchema, PropertySchema
from model import InsightDatasource, InsightNsConversation, InsightNsRelDatasource
from utils import dump_json, normalize_datasource_type


class InsightNsRelDatasourceService:
    """负责管理会话级数据源绑定关系。"""

    def __init__(self, session: Session):
        self.session = session

    def find_by_conversation_id(self, insight_conversation_id: int) -> list[dict[str, Any]]:
        rows = self.session.query(InsightNsRelDatasource, InsightDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).order_by(
            InsightNsRelDatasource.sort_no.asc(),
            InsightNsRelDatasource.id.asc(),
        ).all()
        return [self._to_dict(rel, datasource) for rel, datasource in rows]

    def find_by_namespace_id(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.session.query(InsightDatasource).filter(
            InsightDatasource.insight_namespace_id == insight_namespace_id,
            InsightDatasource.is_deleted == 0,
        ).order_by(
            InsightDatasource.created_at.desc(),
            InsightDatasource.id.desc(),
        ).all()

        bound_datasource_ids: set[int] = set()
        if insight_conversation_id:
            bound_rows = self.session.query(InsightNsRelDatasource.datasource_id).filter(
                InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
                InsightNsRelDatasource.is_deleted == 0,
            ).all()
            bound_datasource_ids = {int(row[0]) for row in bound_rows}

        return [
            self._datasource_to_dict(
                row,
                checked=row.id in bound_datasource_ids,
                insight_conversation_id=insight_conversation_id or 0,
            )
            for row in rows
        ]

    def bind_existing_datasource(self, insight_conversation_id: int, datasource_id: int) -> dict[str, Any]:
        conversation = self._get_conversation(insight_conversation_id)
        if conversation is None:
            return {"success": False, "message": "会话不存在"}

        datasource = self.session.query(InsightDatasource).filter(
            InsightDatasource.id == datasource_id,
            InsightDatasource.insight_namespace_id == conversation.insight_namespace_id,
            InsightDatasource.is_deleted == 0,
        ).first()
        if datasource is None:
            return {"success": False, "message": "数据源不存在"}

        existing = self.session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == datasource.id,
            InsightNsRelDatasource.is_deleted == 0,
        ).first()
        if existing is not None:
            return {"success": True, "message": "数据源已绑定到当前会话", "data": self._to_dict(existing, datasource)}

        relation = InsightNsRelDatasource(
            insight_namespace_id=conversation.insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            datasource_id=datasource.id,
            is_active=1,
            sort_no=self._next_sort_no(insight_conversation_id),
        )
        self.session.add(relation)
        self.session.commit()
        return {"success": True, "message": "数据源已绑定到当前会话", "data": self._to_dict(relation, datasource)}

    def upload_file_datasource_to_namespace(
        self,
        insight_namespace_id: int,
        upload_file: FileStorage,
        upload_dir: str,
    ) -> dict[str, Any]:
        if insight_namespace_id <= 0:
            return {"success": False, "message": "空间不存在"}
        if upload_file is None or not upload_file.filename:
            return {"success": False, "message": "请选择要上传的文件"}

        original_filename = Path(upload_file.filename).name
        suffix = Path(original_filename).suffix.lower()
        if suffix not in {'.csv', '.xls', '.xlsx'}:
            return {"success": False, "message": "仅支持上传 csv、xls、xlsx 文件"}

        upload_dir_path = Path(upload_dir)
        upload_dir_path.mkdir(parents=True, exist_ok=True)
        stored_filename = f"{uuid4().hex}{suffix}"
        stored_path = upload_dir_path / stored_filename
        upload_file.save(str(stored_path))

        datasource_name = self._build_unique_datasource_name(
            insight_namespace_id=insight_namespace_id,
            base_name=Path(original_filename).stem or f"外部数据源_{uuid4().hex[:6]}",
        )
        try:
            datasource_schema = self._build_file_datasource_schema(stored_path, datasource_name)
        except ValueError as exc:
            if stored_path.exists():
                stored_path.unlink(missing_ok=True)
            return {"success": False, "message": str(exc)}
        datasource_config_json = dump_json({
            "file_path": str(stored_path).replace('\\', '/'),
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "file_extension": suffix.lstrip('.'),
        })
        knowledge_tag = f"upload_{uuid4().hex[:16]}"

        datasource = self._get_or_create_datasource(
            insight_namespace_id=insight_namespace_id,
            datasource_type='local_file',
            datasource_name=datasource_name,
            knowledge_tag=knowledge_tag,
            datasource_schema=datasource_schema,
            datasource_config_json=datasource_config_json,
        )
        self.session.commit()
        return {"success": True, "message": "文件上传成功", "data": self._datasource_to_dict(datasource)}

    def delete_namespace_datasource(self, insight_namespace_id: int, datasource_id: int) -> dict[str, Any]:
        datasource = self.session.query(InsightDatasource).filter(
            InsightDatasource.id == datasource_id,
            InsightDatasource.insight_namespace_id == insight_namespace_id,
            InsightDatasource.is_deleted == 0,
        ).first()
        if datasource is None:
            return {"success": False, "message": "数据源不存在"}

        reference_count = self.session.query(InsightNsRelDatasource.id).filter(
            InsightNsRelDatasource.datasource_id == datasource_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).count()
        if reference_count > 0:
            return {
                "success": False,
                "message": f"当前数据源已被 {reference_count} 个会话引用，请先解绑后再删除",
            }

        datasource.is_deleted = 1
        self.session.commit()
        return {"success": True, "message": "数据源删除成功"}

    def remove_datasource(self, insight_conversation_id: int, datasource_id: int) -> dict[str, Any]:
        row = self.session.query(InsightNsRelDatasource, InsightDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
            InsightDatasource.id == datasource_id,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).first()
        if not row:
            return {"success": False, "message": "数据源关系不存在"}

        relation, datasource = row
        relation.is_deleted = 1

        self.session.commit()
        return {"success": True, "message": "移除成功"}

    def _get_conversation(self, insight_conversation_id: int) -> InsightNsConversation | None:
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == insight_conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()

    def _namespace_has_same_named_datasource(self, insight_namespace_id: int, datasource_name: str) -> bool:
        existing = self.session.query(InsightDatasource.id).filter(
            InsightDatasource.insight_namespace_id == insight_namespace_id,
            InsightDatasource.datasource_name == datasource_name,
            InsightDatasource.is_deleted == 0,
        ).first()
        return existing is not None

    def _build_unique_datasource_name(self, insight_namespace_id: int, base_name: str) -> str:
        normalized_base = (base_name or '外部数据源').strip()[:128] or '外部数据源'
        if not self._namespace_has_same_named_datasource(insight_namespace_id, normalized_base):
            return normalized_base

        suffix = 2
        while True:
            candidate = f"{normalized_base}_{suffix}"[:128]
            if not self._namespace_has_same_named_datasource(insight_namespace_id, candidate):
                return candidate
            suffix += 1

    def _next_sort_no(self, insight_conversation_id: int) -> int:
        count = self.session.query(InsightNsRelDatasource.id).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).count()
        return count + 1

    def _build_file_datasource_schema(self, file_path: Path, datasource_name: str) -> str:
        dataframe = self._read_file_preview(file_path)

        properties: dict[str, PropertySchema] = {}
        for column in dataframe.columns:
            series = dataframe[column]
            properties[str(column)] = PropertySchema(
                type=self._infer_schema_type(series),
                description=f"文件字段“{column}”",
                example=self._extract_example_value(series),
            )

        schema = DataSourceSchema(
            name=datasource_name,
            description=f"用户上传的 {file_path.suffix.lstrip('.').upper()} 文件",
            properties=properties,
            required=[str(column) for column in dataframe.columns],
        )
        return dump_json(schema.model_dump())

    def _read_file_preview(self, file_path: Path) -> pd.DataFrame:
        try:
            if file_path.suffix.lower() == '.csv':
                return pd.read_csv(file_path, nrows=50)
            if file_path.suffix.lower() == '.xlsx':
                return pd.read_excel(file_path, nrows=50, engine='openpyxl')
            if file_path.suffix.lower() == '.xls':
                return pd.read_excel(file_path, nrows=50, engine='xlrd')
            raise ValueError(f'暂不支持解析 {file_path.suffix} 文件')
        except ImportError as exc:
            raise ValueError(f'文件解析依赖缺失：{exc}') from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f'文件解析失败：{exc}') from exc

    def _infer_schema_type(self, series: pd.Series) -> str:
        dtype = str(series.dtype).lower()
        if 'int' in dtype:
            return 'integer'
        if 'float' in dtype or 'double' in dtype or 'decimal' in dtype:
            return 'number'
        if 'bool' in dtype:
            return 'boolean'
        if 'date' in dtype or 'time' in dtype:
            return 'string'
        return 'string'

    def _extract_example_value(self, series: pd.Series) -> Any | None:
        non_null = series.dropna()
        if non_null.empty:
            return None
        value = non_null.iloc[0]
        if hasattr(value, 'isoformat'):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        if isinstance(value, (int, float, bool)):
            return value
        return str(value)

    def _get_or_create_datasource(
        self,
        insight_namespace_id: int,
        datasource_type: str,
        datasource_name: str,
        knowledge_tag: str,
        datasource_schema: str,
        datasource_config_json: str,
    ) -> InsightDatasource:
        normalized_type = normalize_datasource_type(datasource_type)
        if normalized_type == 'unknown':
            raise ValueError(f'不支持的数据源类型: {datasource_type}')

        datasource = self.session.query(InsightDatasource).filter(
            InsightDatasource.insight_namespace_id == insight_namespace_id,
            InsightDatasource.datasource_name == datasource_name,
            InsightDatasource.datasource_type == normalized_type,
            InsightDatasource.is_deleted == 0,
        ).first()
        if datasource is not None:
            datasource.knowledge_tag = knowledge_tag or datasource.knowledge_tag or ''
            datasource.datasource_schema = datasource_schema or datasource.datasource_schema or ''
            datasource.datasource_config_json = datasource_config_json or datasource.datasource_config_json or '{}'
            return datasource

        datasource = InsightDatasource(
            insight_namespace_id=insight_namespace_id,
            datasource_type=normalized_type,
            datasource_name=datasource_name,
            knowledge_tag=knowledge_tag or '',
            datasource_schema=datasource_schema or '',
            datasource_config_json=datasource_config_json or '{}',
        )
        self.session.add(datasource)
        self.session.flush()
        return datasource

    def _to_dict(self, rel: InsightNsRelDatasource, datasource: InsightDatasource) -> dict[str, Any]:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "insight_conversation_id": rel.insight_conversation_id,
            "datasource_id": datasource.id,
            "datasource_type": datasource.datasource_type,
            "datasource_name": datasource.datasource_name,
            "knowledge_tag": datasource.knowledge_tag,
            "datasource_schema": datasource.datasource_schema,
            "datasource_config_json": datasource.datasource_config_json,
            "sort_no": rel.sort_no,
            "created_at": rel.created_at.isoformat() if rel.created_at else None,
            "updated_at": rel.updated_at.isoformat() if rel.updated_at else None,
        }

    def _datasource_to_dict(
        self,
        datasource: InsightDatasource,
        checked: bool = False,
        insight_conversation_id: int = 0,
    ) -> dict[str, Any]:
        return {
            "id": datasource.id,
            "datasource_id": datasource.id,
            "insight_namespace_id": datasource.insight_namespace_id,
            "insight_conversation_id": insight_conversation_id,
            "datasource_type": datasource.datasource_type,
            "datasource_name": datasource.datasource_name,
            "knowledge_tag": datasource.knowledge_tag,
            "datasource_schema": datasource.datasource_schema,
            "datasource_config_json": datasource.datasource_config_json,
            "checked": checked,
            "created_at": datasource.created_at.isoformat() if datasource.created_at else None,
            "updated_at": datasource.updated_at.isoformat() if datasource.updated_at else None,
        }
