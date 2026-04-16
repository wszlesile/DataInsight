from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore
from uuid import uuid4
from typing import Any

import pandas as pd
from werkzeug.datastructures import FileStorage

from api import supos_kernel_api
from config.config import Config
from sqlalchemy.orm import Session

from dto import DataSourceSchema, PropertySchema
from model import InsightDatasource, InsightNsConversation, InsightNsRelDatasource, InsightNsUnsSelection
from utils import dump_json, normalize_datasource_type, safe_json_loads

SHARED_UNS_NAMESPACE_ID = 0
DEFAULT_CONVERSATION_ID = 0
BIND_SOURCE_USER_SELECTED = 'user_selected'
BIND_SOURCE_IMPORT_SELECTED = 'import_selected'
BIND_SOURCE_SYSTEM_DEFAULT = 'system_default'
UNS_MAX_EXPANDED_FILES = Config.UNS_MAX_EXPANDED_FILES
UNS_MAX_EXPAND_DEPTH = Config.UNS_MAX_EXPAND_DEPTH
UNS_TREE_PAGE_SIZE = Config.UNS_TREE_PAGE_SIZE
UNS_DETAIL_WORKERS = Config.UNS_DETAIL_WORKERS
UNS_IMPORT_MAX_CONCURRENT = Config.UNS_IMPORT_MAX_CONCURRENT
UNS_IMPORT_SEMAPHORE = BoundedSemaphore(UNS_IMPORT_MAX_CONCURRENT)


@contextmanager
def _uns_import_slot():
    acquired = UNS_IMPORT_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        raise RuntimeError('UNS 节点导入操作繁忙，请稍后再试')
    try:
        yield
    finally:
        UNS_IMPORT_SEMAPHORE.release()


class InsightNsRelDatasourceService:
    """管理空间级数据源以及会话级绑定关系。"""

    def __init__(self, session: Session):
        self.session = session

    def find_by_conversation_id(self, insight_conversation_id: int) -> list[dict[str, Any]]:
        """查询某个会话已绑定的数据源。"""
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
        """
        查询空间级数据源列表。

        如果传入会话 ID，返回结果会直接附带 `checked`，供前端勾选框展示。
        """
        bound_datasource_ids: set[int] = set()
        if insight_conversation_id:
            bound_rows = self.session.query(
                InsightNsRelDatasource.datasource_id,
                InsightNsRelDatasource.bind_source,
            ).filter(
                InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
                InsightNsRelDatasource.is_deleted == 0,
            ).all()
            bound_datasource_ids = {
                int(row[0])
                for row in bound_rows
                if (row[1] or BIND_SOURCE_USER_SELECTED) != BIND_SOURCE_SYSTEM_DEFAULT
            }

        rows = self.session.query(InsightDatasource).filter(
            InsightDatasource.is_deleted == 0,
            InsightDatasource.insight_namespace_id == insight_namespace_id,
        ).order_by(
            InsightDatasource.created_at.desc(),
            InsightDatasource.id.desc(),
        ).all()

        return [
            self._datasource_to_dict(
                row,
                checked=row.id in bound_datasource_ids,
                insight_conversation_id=insight_conversation_id or 0,
            )
            for row in rows
        ]

    def bind_existing_datasource(self, insight_conversation_id: int, datasource_id: int) -> dict[str, Any]:
        """把一条空间级数据源绑定到会话。"""
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
            bind_source=BIND_SOURCE_USER_SELECTED,
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
        """
        上传文件并在空间下创建数据源。

        该方法只负责空间级资源创建，不会自动把数据源绑定到任何会话。
        """
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

    def update_namespace_datasource_description(
        self,
        insight_namespace_id: int,
        datasource_id: int,
        description: str,
    ) -> dict[str, Any]:
        """
        更新空间数据源描述。

        描述存放在 datasource_schema.description，因此这里只改这一项，
        避免编辑描述时覆盖其他 schema 元信息。
        """
        datasource = self.session.query(InsightDatasource).filter(
            InsightDatasource.id == datasource_id,
            InsightDatasource.insight_namespace_id.in_([SHARED_UNS_NAMESPACE_ID, insight_namespace_id]),
            InsightDatasource.is_deleted == 0,
        ).first()
        if datasource is None:
            return {"success": False, "message": "数据源不存在"}

        schema_payload = safe_json_loads(datasource.datasource_schema, {})
        if not isinstance(schema_payload, dict):
            schema_payload = {}
        schema_payload['description'] = str(description or '').strip()

        datasource.datasource_schema = dump_json(schema_payload)
        self.session.commit()
        self.session.refresh(datasource)
        return {
            "success": True,
            "message": "数据源描述已更新",
            "data": self._datasource_to_dict(datasource),
        }

    def import_uns_nodes_to_namespace(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int = 0,
        nodes: list[dict[str, Any]] | None = None,
        authorization: str = '',
        lake_rds_database_name: str = '',
    ) -> dict[str, Any]:
        """
        将选中的 UNS 节点转换为共享 table 数据源，并绑定到当前会话。

        UNS 数据源本体只在虚拟共享空间落一份；当前会话通过关系表获得自己的
        绑定记录，分析链路仍然只按当前会话绑定查询。
        """
        if insight_namespace_id <= 0:
            return {"success": False, "message": "空间不存在"}
        conversation = self._get_conversation(insight_conversation_id)
        if conversation is None or conversation.insight_namespace_id != insight_namespace_id:
            return {"success": False, "message": "当前会话不存在或不属于该空间"}

        normalized_nodes = self._normalize_uns_nodes(nodes)

        try:
            with _uns_import_slot():
                imported_rows: list[dict[str, Any]] = []
                failed_rows: list[dict[str, Any]] = []
                desired_selections: list[dict[str, Any]] = []
                desired_file_node_ids: list[str] = []
                desired_datasource_ids: list[int] = []

                if normalized_nodes:
                    if not authorization:
                        return {"success": False, "message": "缺少 SUPOS 认证信息，无法导入 UNS 节点"}
                    if not lake_rds_database_name:
                        return {"success": False, "message": "当前用户上下文未初始化 LakeRDS 数据库名"}

                    expand_result = self._expand_uns_file_nodes(normalized_nodes, authorization)
                    if expand_result["failed"]:
                        return {
                            "success": False,
                            "message": self._build_uns_failure_message(
                                expand_result["failed"],
                                "UNS \u8282\u70b9\u540c\u6b65\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5",
                            ),
                            "data": {
                                "imported": [],
                                "failed": expand_result["failed"],
                                "selections": self.list_uns_selections(insight_conversation_id),
                            },
                        }

                    desired_selections = self._canonicalize_uns_selections(expand_result["selections"])
                    desired_file_node_ids = self._normalize_uns_node_id_list([
                        node.get("id")
                        for selection in desired_selections
                        for node in [{"id": item} for item in (selection.get("expanded_uns_node_ids") or [])]
                    ])
                    if not desired_file_node_ids:
                        self._replace_uns_selections(
                            insight_namespace_id=insight_namespace_id,
                            insight_conversation_id=insight_conversation_id,
                            selections=[],
                        )
                        removed_datasource_ids = self._sync_uns_conversation_bindings(
                            insight_namespace_id=insight_namespace_id,
                            insight_conversation_id=insight_conversation_id,
                            desired_datasource_ids=[],
                        )
                        self.session.commit()
                        return {
                            "success": True,
                            "message": "已清空 UNS 节点选择",
                            "data": {
                                "imported": [],
                                "failed": [],
                                "removed_datasource_ids": removed_datasource_ids,
                                "selections": self.list_uns_selections(insight_conversation_id),
                            },
                        }

                    existing_datasources = self._find_uns_datasources_by_node_ids(desired_file_node_ids)
                    missing_node_ids = [
                        node_id
                        for node_id in desired_file_node_ids
                        if node_id not in existing_datasources
                    ]
                    if missing_node_ids:
                        detail_results = self._fetch_uns_details_in_parallel(
                            missing_node_ids,
                            authorization,
                        )
                        failed_rows.extend([
                            {
                                "id": result.get("id", ""),
                                "message": result.get("message", "导入失败"),
                            }
                            for result in detail_results
                            if not result.get("success")
                        ])
                        if failed_rows:
                            return {
                                "success": False,
                                "message": self._build_uns_failure_message(
                                    failed_rows,
                                    "UNS 节点同步失败，请稍后重试",
                                ),
                                "data": {
                                    "imported": [],
                                    "failed": failed_rows,
                                    "selections": self.list_uns_selections(insight_conversation_id),
                                },
                            }

                        for result in detail_results:
                            datasource = self._upsert_uns_table_datasource(
                                detail=result["detail"],
                                lake_rds_database_name=lake_rds_database_name,
                            )
                            self._bind_datasource(
                                insight_namespace_id=SHARED_UNS_NAMESPACE_ID,
                                insight_conversation_id=DEFAULT_CONVERSATION_ID,
                                datasource_id=datasource.id,
                                bind_source=BIND_SOURCE_SYSTEM_DEFAULT,
                            )
                            imported_rows.append(self._datasource_to_dict(datasource))

                    desired_datasource_ids = self._find_datasource_ids_by_uns_node_ids(desired_file_node_ids)

                self._replace_uns_selections(
                    insight_namespace_id=insight_namespace_id,
                    insight_conversation_id=insight_conversation_id,
                    selections=desired_selections,
                )
                removed_datasource_ids = self._sync_uns_conversation_bindings(
                    insight_namespace_id=insight_namespace_id,
                    insight_conversation_id=insight_conversation_id,
                    desired_datasource_ids=desired_datasource_ids,
                )
        except RuntimeError as exc:
            return {"success": False, "message": str(exc)}

        self.session.commit()
        message = "UNS 节点选择已同步"
        if imported_rows:
            message = f"UNS 节点选择已同步，新增导入 {len(imported_rows)} 个数据源"
        return {
            "success": True,
            "message": message,
            "data": {
                "imported": imported_rows,
                "failed": failed_rows,
                "removed_datasource_ids": removed_datasource_ids,
                "selections": self.list_uns_selections(insight_conversation_id),
            },
        }

    def _build_uns_failure_message(
        self,
        failed_rows: list[dict[str, Any]],
        default_message: str,
    ) -> str:
        if not failed_rows:
            return default_message

        first_message = str((failed_rows[0] or {}).get("message") or "").strip()
        if not first_message:
            return default_message
        if len(failed_rows) == 1:
            return first_message
        return (
            f"{first_message}\uff1b"
            f"\u53e6\u6709 {len(failed_rows) - 1} \u4e2a\u8282\u70b9\u5904\u7406\u5931\u8d25"
        )


    def list_uns_selections(self, insight_conversation_id: int) -> list[dict[str, Any]]:
        rows = self.session.query(InsightNsUnsSelection).filter(
            InsightNsUnsSelection.insight_conversation_id == insight_conversation_id,
            InsightNsUnsSelection.is_deleted == 0,
        ).order_by(
            InsightNsUnsSelection.id.asc(),
        ).all()
        selected_rows: list[dict[str, Any]] = []
        selected_node_ids: set[str] = set()
        derived_partial_ids: set[str] = set()

        for row in rows:
            payload = row.to_dict()
            payload["selection_state"] = "selected"
            payload["derived"] = False
            selected_rows.append(payload)
            selected_node_ids.add(str(payload.get("uns_node_id") or "").strip())

        partial_rows: list[dict[str, Any]] = []
        for payload in selected_rows:
            for ancestor_id in self._normalize_uns_node_id_list(payload.get("uns_node_path") or []):
                if ancestor_id in selected_node_ids or ancestor_id in derived_partial_ids:
                    continue
                partial_rows.append({
                    "id": f"partial:{ancestor_id}",
                    "insight_namespace_id": payload.get("insight_namespace_id"),
                    "insight_conversation_id": payload.get("insight_conversation_id"),
                    "uns_node_id": ancestor_id,
                    "uns_node_name": "",
                    "uns_node_path": [],
                    "is_folder": True,
                    "expanded_uns_node_ids": [],
                    "selection_state": "partial",
                    "derived": True,
                    "is_deleted": 0,
                    "created_at": payload.get("created_at"),
                    "updated_at": payload.get("updated_at"),
                })
                derived_partial_ids.add(ancestor_id)
        return selected_rows + partial_rows

    def delete_namespace_datasource(self, insight_namespace_id: int, datasource_id: int) -> dict[str, Any]:
        """删除空间级数据源；若仍被会话引用，则阻止删除。"""
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
        """从会话解绑数据源，不删除空间级数据源本体。"""
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

    def _normalize_uns_nodes(
        self,
        nodes: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()

        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get('id') or node.get('uns_node_id') or '').strip()
            if not node_id or node_id in seen:
                continue
            count_children = int(node.get('countChildren') or node.get('count_children') or 0)
            is_folder = bool(node.get('isFolder') or node.get('is_folder'))
            if not is_folder:
                is_folder = bool(node.get('hasChildren')) or count_children > 0
            normalized.append({
                "id": node_id,
                "alias": str(node.get('alias') or '').strip(),
                "name": str(node.get('name') or node.get('label') or node.get('pathName') or node_id).strip(),
                "uns_node_path": self._normalize_uns_node_id_list(
                    node.get('unsNodePath')
                    or node.get('uns_node_path')
                    or node.get('ancestorNodeIds')
                    or node.get('ancestor_node_ids')
                    or []
                ),
                "count_children": count_children,
                "is_folder": is_folder,
            })
            seen.add(node_id)
        return normalized

    def _expand_uns_file_nodes(
        self,
        selected_nodes: list[dict[str, Any]],
        authorization: str,
    ) -> dict[str, list[dict[str, Any]]]:
        file_nodes: list[dict[str, Any]] = []
        selections: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        seen_file_ids: set[str] = set()

        for node in selected_nodes:
            node_id = node["id"]
            if not node.get("is_folder"):
                if node_id not in seen_file_ids:
                    file_nodes.append(node)
                    seen_file_ids.add(node_id)
                selections.append({
                    **node,
                    "expanded_uns_node_ids": [node_id],
                })
                continue

            try:
                if int(node.get("count_children") or 0) > UNS_MAX_EXPANDED_FILES:
                    raise ValueError(
                        f"UNS 文件夹“{node.get('name') or node_id}”子节点过多（{node.get('count_children')}），"
                        f"请继续展开后选择更小范围（当前上限 {UNS_MAX_EXPANDED_FILES}）"
                    )
                expanded = self._expand_uns_folder_files(node, authorization)
                for file_node in expanded:
                    file_id = file_node["id"]
                    if file_id in seen_file_ids:
                        continue
                    file_nodes.append(file_node)
                    seen_file_ids.add(file_id)
                selections.append({
                    **node,
                    "expanded_uns_node_ids": [item["id"] for item in expanded],
                })
            except Exception as exc:
                failed.append({
                    "id": node_id,
                    "name": node.get("name", ""),
                    "message": str(exc),
                })

        return {
            "file_nodes": file_nodes,
            "selections": selections,
            "failed": failed,
        }

    def _expand_uns_folder_files(self, root_node: dict[str, Any], authorization: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        stack: list[tuple[dict[str, Any], int]] = [(root_node, 0)]

        while stack:
            folder, depth = stack.pop()
            if depth >= UNS_MAX_EXPAND_DEPTH:
                raise ValueError(f"UNS 文件夹展开深度超过限制 {UNS_MAX_EXPAND_DEPTH}")

            for child in self._fetch_all_uns_children(folder["id"], authorization):
                child_id = str(child.get('id') or child.get('alias') or '').strip()
                if not child_id:
                    continue
                child_node = {
                    "id": child_id,
                    "alias": str(child.get('alias') or '').strip(),
                    "name": str(child.get('name') or child.get('pathName') or child_id).strip(),
                    "path": str(child.get('pathName') or child.get('path') or '').strip(),
                    "is_folder": bool(child.get('hasChildren'))
                    or int(child.get('countChildren') or 0) > 0
                    or int(child.get('type') if child.get('type') is not None else -1) == 0,
                }
                if child_node["is_folder"]:
                    stack.append((child_node, depth + 1))
                    continue

                files.append(child_node)
                if len(files) > UNS_MAX_EXPANDED_FILES:
                    raise ValueError(f"UNS 文件夹展开文件数超过限制 {UNS_MAX_EXPANDED_FILES}")

        return files

    def _fetch_all_uns_children(self, parent_id: str, authorization: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_no = 1
        total = None

        while True:
            payload = supos_kernel_api.fetch_uns_tree_nodes(
                authorization=authorization,
                parent_id=parent_id,
                page_no=page_no,
                page_size=UNS_TREE_PAGE_SIZE,
                keyword='',
                search_type=1,
            )
            page_rows = payload.get("data") or []
            rows.extend(page_rows)
            total = int(payload.get("total") or len(rows)) if total is None else total

            if not page_rows or len(rows) >= total:
                break
            page_no += 1
            if page_no > 100:
                raise ValueError("UNS 文件夹分页过多，已停止展开")

        return rows

    def _fetch_uns_details_in_parallel(
        self,
        ids: list[str],
        authorization: str,
    ) -> list[dict[str, Any]]:
        if not ids:
            return []

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(UNS_DETAIL_WORKERS, max(1, len(ids)))) as executor:
            future_to_id = {
                executor.submit(supos_kernel_api.fetch_uns_instance_detail, node_id, authorization): node_id
                for node_id in ids
            }
            for future in as_completed(future_to_id):
                node_id = future_to_id[future]
                try:
                    detail = future.result()
                    if not str(detail.get('id') or '').strip():
                        detail = {**detail, "id": node_id}
                    results.append({
                        "success": True,
                        "id": node_id,
                        "detail": detail,
                    })
                except Exception as exc:
                    results.append({
                        "success": False,
                        "id": node_id,
                        "message": str(exc),
                    })
        results.sort(key=lambda item: ids.index(item["id"]))
        return results

    def _upsert_uns_table_datasource(
        self,
        detail: dict[str, Any],
        lake_rds_database_name: str,
    ) -> InsightDatasource:
        uns_node_id = str(detail.get('id') or '').strip()
        if not uns_node_id:
            raise ValueError('UNS 节点缺少 id，无法导入')
        alias = str(detail.get('alias') or '').strip()
        if not alias:
            raise ValueError('UNS 节点缺少 alias，无法导入')

        datasource = self._find_uns_datasource_by_node_id(uns_node_id)
        datasource_name = str(detail.get('name') or alias).strip()[:128] or alias

        if datasource is None:
            datasource_name = self._build_unique_datasource_name(SHARED_UNS_NAMESPACE_ID, datasource_name)
            datasource = InsightDatasource(
                insight_namespace_id=SHARED_UNS_NAMESPACE_ID,
                datasource_type='table',
                datasource_name=datasource_name,
                knowledge_tag='',
                uns_node_id=uns_node_id,
                datasource_schema='',
                datasource_config_json='{}',
            )
            self.session.add(datasource)
            self.session.flush()
        else:
            datasource.datasource_name = datasource_name
            datasource.uns_node_id = uns_node_id

        datasource.datasource_schema = self._build_uns_datasource_schema(detail, datasource.datasource_name)
        datasource.datasource_config_json = dump_json({
            "database_name": lake_rds_database_name,
            "table_name": f"public.{alias}",
            "uns_alias": alias,
            "uns_path": detail.get('path') or '',
            "uns_path_name": detail.get('pathName') or detail.get('name') or '',
        })
        return datasource

    def _find_uns_datasource_by_node_id(
        self,
        uns_node_id: str,
    ) -> InsightDatasource | None:
        return self.session.query(InsightDatasource).filter(
            InsightDatasource.uns_node_id == uns_node_id,
            InsightDatasource.datasource_type == 'table',
            InsightDatasource.is_deleted == 0,
        ).first()

    def _find_datasource_ids_by_uns_node_ids(self, uns_node_ids: list[str]) -> list[int]:
        normalized_ids = [
            str(item or '').strip()
            for item in uns_node_ids or []
            if str(item or '').strip()
        ]
        if not normalized_ids:
            return []

        rows = self.session.query(InsightDatasource.id).filter(
            InsightDatasource.uns_node_id.in_(normalized_ids),
            InsightDatasource.datasource_type == 'table',
            InsightDatasource.is_deleted == 0,
        ).all()
        return [int(row[0]) for row in rows]

    def _soft_delete_conversation_datasource_relations(
        self,
        insight_conversation_id: int,
        datasource_ids: list[int],
    ) -> None:
        if not datasource_ids:
            return
        self.session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id.in_(datasource_ids),
            InsightNsRelDatasource.is_deleted == 0,
        ).update(
            {InsightNsRelDatasource.is_deleted: 1},
            synchronize_session=False,
        )

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

    def _bind_datasource(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int,
        datasource_id: int,
        bind_source: str,
    ) -> InsightNsRelDatasource:
        relation = self.session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == datasource_id,
        ).first()
        if relation is not None:
            relation.insight_namespace_id = insight_namespace_id
            relation.is_deleted = 0
            relation.is_active = 1
            relation.bind_source = bind_source
            if not relation.sort_no:
                relation.sort_no = self._next_sort_no(insight_conversation_id)
            return relation

        relation = InsightNsRelDatasource(
            insight_namespace_id=insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            datasource_id=datasource_id,
            is_active=1,
            sort_no=self._next_sort_no(insight_conversation_id),
            bind_source=bind_source,
        )
        self.session.add(relation)
        self.session.flush()
        return relation

    def _upsert_uns_selection(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int,
        selection: dict[str, Any],
    ) -> InsightNsUnsSelection:
        row = self.session.query(InsightNsUnsSelection).filter(
            InsightNsUnsSelection.insight_conversation_id == insight_conversation_id,
            InsightNsUnsSelection.uns_node_id == selection["id"],
        ).first()
        if row is None:
            row = InsightNsUnsSelection(
                insight_namespace_id=insight_namespace_id,
                insight_conversation_id=insight_conversation_id,
                uns_node_id=selection["id"],
            )
            self.session.add(row)

        row.insight_namespace_id = insight_namespace_id
        row.uns_node_name = selection.get("name", "")[:255]
        row.uns_node_path = self._build_uns_selection_path_payload(selection.get("uns_node_path") or [])
        row.is_folder = 1 if selection.get("is_folder") else 0
        row.expanded_uns_node_ids_json = dump_json(selection.get("expanded_uns_node_ids") or [])
        row.is_deleted = 0
        self.session.flush()
        return row

    def _canonicalize_uns_selections(
        self,
        selections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_selections: list[dict[str, Any]] = []
        for selection in selections or []:
            selection_id = str(selection.get("id") or "").strip()
            if not selection_id:
                continue
            normalized_selections.append({
                **selection,
                "id": selection_id,
                "expanded_uns_node_ids": self._normalize_uns_node_id_list(selection.get("expanded_uns_node_ids") or [selection_id]),
            })

        sorted_selections = sorted(
            normalized_selections,
            key=lambda item: (
                0 if item.get("is_folder") else 1,
                -len(item.get("expanded_uns_node_ids") or []),
                item["id"],
            ),
        )
        kept: list[dict[str, Any]] = []
        covered_node_ids: set[str] = set()
        for selection in sorted_selections:
            selection_id = selection["id"]
            expanded_ids = selection.get("expanded_uns_node_ids") or [selection_id]
            if selection_id in covered_node_ids or set(expanded_ids).issubset(covered_node_ids):
                continue
            kept.append(selection)
            covered_node_ids.update(expanded_ids)
            covered_node_ids.add(selection_id)
        return kept

    def _normalize_uns_node_id_list(self, values: list[Any]) -> list[str]:
        normalized_ids: list[str] = []
        seen: set[str] = set()
        for item in values:
            node_id = str(item or '').strip()
            if not node_id or node_id in seen:
                continue
            normalized_ids.append(node_id)
            seen.add(node_id)
        return normalized_ids

    def _build_uns_selection_path_payload(
        self,
        uns_node_path: list[Any],
    ) -> str:
        return dump_json(self._normalize_uns_node_id_list(uns_node_path or []))

    def _upsert_uns_file_selection_by_node_id(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int,
        uns_node_id: str,
    ) -> InsightNsUnsSelection:
        datasource = self._find_uns_datasource_by_node_id(uns_node_id)
        datasource_config = safe_json_loads(datasource.datasource_config_json, {}) if datasource else {}
        return self._upsert_uns_selection(
            insight_namespace_id=insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            selection={
                "id": uns_node_id,
                "name": datasource.datasource_name if datasource else uns_node_id,
                "uns_node_path": [],
                "is_folder": False,
                "expanded_uns_node_ids": [uns_node_id],
            },
        )

    def _replace_uns_selections(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int,
        selections: list[dict[str, Any]],
    ) -> None:
        self.session.query(InsightNsUnsSelection).filter(
            InsightNsUnsSelection.insight_conversation_id == insight_conversation_id,
            InsightNsUnsSelection.is_deleted == 0,
        ).update(
            {InsightNsUnsSelection.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.flush()

        for selection in selections:
            self._upsert_uns_selection(
                insight_namespace_id=insight_namespace_id,
                insight_conversation_id=insight_conversation_id,
                selection=selection,
            )

    def _find_uns_datasources_by_node_ids(
        self,
        uns_node_ids: list[str],
    ) -> dict[str, InsightDatasource]:
        normalized_ids = self._normalize_uns_node_id_list(uns_node_ids)
        if not normalized_ids:
            return {}

        rows = self.session.query(InsightDatasource).filter(
            InsightDatasource.uns_node_id.in_(normalized_ids),
            InsightDatasource.datasource_type == 'table',
            InsightDatasource.is_deleted == 0,
        ).all()
        return {
            str(row.uns_node_id or '').strip(): row
            for row in rows
            if str(row.uns_node_id or '').strip()
        }

    def _sync_uns_conversation_bindings(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int,
        desired_datasource_ids: list[int],
    ) -> list[int]:
        desired_ids = sorted(set(int(item) for item in desired_datasource_ids or [] if int(item) > 0))
        active_rows = self.session.query(InsightNsRelDatasource.id, InsightNsRelDatasource.datasource_id).join(
            InsightDatasource,
            InsightDatasource.id == InsightNsRelDatasource.datasource_id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.bind_source == BIND_SOURCE_IMPORT_SELECTED,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
            InsightDatasource.uns_node_id != '',
        ).all()

        active_ids = {int(row[1]) for row in active_rows}
        removable_ids = [datasource_id for datasource_id in active_ids if datasource_id not in desired_ids]
        self._soft_delete_conversation_datasource_relations(
            insight_conversation_id=insight_conversation_id,
            datasource_ids=removable_ids,
        )

        for datasource_id in desired_ids:
            self._bind_datasource(
                insight_namespace_id=insight_namespace_id,
                insight_conversation_id=insight_conversation_id,
                datasource_id=datasource_id,
                bind_source=BIND_SOURCE_IMPORT_SELECTED,
            )
        return removable_ids

    def _next_sort_no(self, insight_conversation_id: int) -> int:
        count = self.session.query(InsightNsRelDatasource.id).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).count()
        return count + 1

    def _build_file_datasource_schema(self, file_path: Path, datasource_name: str) -> str:
        """读取上传文件前几行数据，并推断元数据 Schema。"""
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

    def _build_uns_datasource_schema(self, detail: dict[str, Any], datasource_name: str) -> str:
        definition = detail.get('fields') or []
        properties: dict[str, PropertySchema] = {}
        required: list[str] = []

        for field in definition:
            field_name = str(field.get('name') or '').strip()
            if not field_name:
                continue

            description_parts = [
                str(field.get('displayName') or '').strip(),
                str(field.get('remark') or '').strip(),
            ]
            properties[field_name] = PropertySchema(
                type=self._map_uns_field_type(field.get('type')),
                description='；'.join(part for part in description_parts if part) or field_name,
                example=None,
            )
            if not bool(field.get('systemField')):
                required.append(field_name)

        schema = DataSourceSchema(
            name=datasource_name,
            description=str(detail.get('description') or '').strip()
            or f"UNS 节点“{detail.get('name') or datasource_name}”转换得到的表结构",
            properties=properties,
            required=required,
        )
        return dump_json(schema.model_dump())

    def _read_file_preview(self, file_path: Path) -> pd.DataFrame:
        """按文件类型读取预览数据；异常会转换成可直接返回给前端的错误。"""
        try:
            if file_path.suffix.lower() == '.csv':
                return self._read_csv_preview(file_path)
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

    def _read_csv_preview(self, file_path: Path) -> pd.DataFrame:
        """优先自动尝试常见 CSV 编码，减少编码差异导致的上传失败。"""
        candidate_encodings = (
            'utf-8',
            'utf-8-sig',
            'gb18030',
            'gbk',
        )
        last_decode_error: UnicodeDecodeError | None = None

        for encoding in candidate_encodings:
            try:
                return pd.read_csv(file_path, nrows=50, encoding=encoding)
            except UnicodeDecodeError as exc:
                last_decode_error = exc
                continue

        if last_decode_error is not None:
            raise ValueError(
                'CSV 文件编码解析失败。系统已自动尝试 UTF-8、UTF-8-SIG、GB18030、GBK，'
                '仍无法读取，请将文件另存为 UTF-8 编码后重试。'
            ) from last_decode_error

        # 不是编码问题时，保留原始异常语义，避免误导用户。
        return pd.read_csv(file_path, nrows=50)

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

    def _map_uns_field_type(self, source_type: Any) -> str:
        normalized = str(source_type or '').strip().upper()
        if normalized in {'INTEGER', 'LONG', 'INT', 'SHORT'}:
            return 'integer'
        if normalized in {'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMBER'}:
            return 'number'
        if normalized in {'BOOLEAN', 'BOOL'}:
            return 'boolean'
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
        """按空间、名称和类型幂等创建数据源定义。"""
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
            "uns_node_id": datasource.uns_node_id,
            "datasource_schema": datasource.datasource_schema,
            "datasource_config_json": datasource.datasource_config_json,
            "sort_no": rel.sort_no,
            "bind_source": rel.bind_source,
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
            "uns_node_id": datasource.uns_node_id,
            "datasource_schema": datasource.datasource_schema,
            "datasource_config_json": datasource.datasource_config_json,
            "checked": checked,
            "created_at": datasource.created_at.isoformat() if datasource.created_at else None,
            "updated_at": datasource.updated_at.isoformat() if datasource.updated_at else None,
        }
