from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config import Config
from config.database import session_scope
from service.conversation_context_service import ConversationContextService
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService
from utils import logger


class SemanticDatasourceSelectionError(RuntimeError):
    """Semantic retrieval could not produce one complete analysis source scope."""


class DatasourceSelectionProviderRouter:
    """Resolve the active provider behind one replaceable routing boundary."""

    @staticmethod
    def resolve(datasource_snapshot: dict[str, Any] | None = None) -> str:
        configured = str(
            getattr(Config, 'DATASOURCE_SELECTION_PROVIDER', 'local') or 'local'
        ).strip().lower()
        if configured in {'semantic', 'semantic_center', 'lightrag'}:
            if DatasourceSelectionProviderRouter._contains_external_datasource(
                datasource_snapshot
            ):
                logger.info(
                    'external datasource is associated with this conversation; '
                    'routing datasource selection to local provider'
                )
                return 'local'
            return 'semantic_center'
        if configured not in {'', 'local'}:
            logger.warning(
                'unknown datasource selection provider=%s; using local selection',
                configured,
            )
        return 'local'

    @staticmethod
    def _contains_external_datasource(
        datasource_snapshot: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(datasource_snapshot, dict):
            return False
        for item in datasource_snapshot.get('selected_datasource_snapshot') or []:
            if not isinstance(item, dict):
                continue
            origin = str(item.get('selection_origin') or '').strip().lower()
            if origin:
                if origin != 'uns':
                    return True
                continue
            # Backward-compatible treatment for snapshots created before the
            # provenance field existed: non-table sources are always external.
            datasource_type = str(
                item.get('datasource_type') or item.get('type') or ''
            ).strip().lower()
            if datasource_type in {'local_file', 'minio_file', 'api'}:
                return True
        return False


@dataclass(frozen=True)
class SemanticDatasourceSelectionResult:
    datasource_ids: tuple[int, ...]
    datasource_snapshot: dict[str, Any]
    relationship_hints: tuple[dict[str, Any], ...]


class SemanticDatasourceSelectionService:
    """Adapt semantic-center UNS node IDs to the canonical datasource snapshot."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        retrieve_path: str | None = None,
        timeout_seconds: float | None = None,
        top_k: int | None = None,
    ) -> None:
        self.base_url = str(base_url or Config.SEMANTIC_BASE).rstrip('/')
        self.retrieve_path = '/' + str(
            retrieve_path or Config.SEMANTIC_DATASOURCE_RETRIEVE_PATH
        ).lstrip('/')
        self.timeout_seconds = float(
            timeout_seconds or Config.SEMANTIC_REQUEST_TIMEOUT_SECONDS
        )
        self.top_k = max(int(top_k or Config.SEMANTIC_DATASOURCE_TOP_K), 1)

    def retrieve(
        self,
        *,
        query: str,
        authorization: str,
        namespace_id: int,
        database_conn_info: dict[str, Any] | None = None,
    ) -> SemanticDatasourceSelectionResult:
        if not query.strip():
            raise SemanticDatasourceSelectionError('semantic selection query is empty')
        if not authorization.strip():
            raise SemanticDatasourceSelectionError('semantic selection requires authorization')

        provider_authorization = str(
            getattr(Config, 'SEMANTIC_DATASOURCE_AUTHORIZATION', '') or authorization
        ).strip()
        try:
            response = requests.post(
                f'{self.base_url}{self.retrieve_path}',
                headers={'Authorization': provider_authorization},
                json={'query': query, 'top_k': self.top_k},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SemanticDatasourceSelectionError(
                f'semantic selection request failed: {exc}'
            ) from exc

        uns_node_ids, relationships = self._validate_payload(payload)
        conn_info = dict(database_conn_info or {})
        try:
            with session_scope(expire_on_commit=False) as session:
                datasource_by_node_id = InsightNsRelDatasourceService(
                    session
                ).materialize_semantic_uns_node_ids(
                    uns_node_ids=uns_node_ids,
                    authorization=authorization,
                    lake_rds_database_name=str(
                        conn_info.get('lake_rds_database_name') or ''
                    ),
                    ts_query_source=str(conn_info.get('ts_query_source') or ''),
                )
                datasource_ids = list(dict.fromkeys(
                    int(datasource_by_node_id[node_id]) for node_id in uns_node_ids
                ))
                snapshot_rows = ConversationContextService(
                    session
                ).build_datasource_snapshot_by_ids(
                    namespace_id=namespace_id,
                    datasource_ids=datasource_ids,
                )
        except Exception as exc:
            raise SemanticDatasourceSelectionError(
                f'semantic datasource materialization failed: {exc}'
            ) from exc

        snapshot_ids = {
            int(item.get('datasource_id') or 0)
            for item in snapshot_rows
            if isinstance(item, dict)
        }
        if snapshot_ids != set(datasource_ids):
            raise SemanticDatasourceSelectionError(
                'semantic datasource snapshot is incomplete after materialization'
            )

        relationship_hints = self._normalize_relationships(
            relationships,
            datasource_by_node_id,
        )
        return SemanticDatasourceSelectionResult(
            datasource_ids=tuple(datasource_ids),
            datasource_snapshot={
                'namespace_id': namespace_id,
                'selected_datasource_ids': datasource_ids,
                'selected_datasource_snapshot': snapshot_rows,
            },
            relationship_hints=tuple(relationship_hints),
        )

    @staticmethod
    def _validate_payload(payload: Any) -> tuple[list[str], list[dict[str, Any]]]:
        if not isinstance(payload, dict):
            raise SemanticDatasourceSelectionError(
                'semantic selection response must be a JSON object'
            )
        raw_entities = payload.get('entity_ids')
        if raw_entities is None:
            raw_entities = payload.get('entities')
        raw_relationships = payload.get('relationships', [])
        if not isinstance(raw_entities, list) or not isinstance(raw_relationships, list):
            raise SemanticDatasourceSelectionError(
                'semantic selection response has invalid entities or relationships'
            )
        uns_node_ids = list(dict.fromkeys(
            str(item or '').strip()
            for item in raw_entities
            if str(item or '').strip()
        ))
        if not uns_node_ids:
            raise SemanticDatasourceSelectionError(
                'semantic selection returned no UNS node IDs'
            )
        relationships = [
            dict(item) for item in raw_relationships if isinstance(item, dict)
        ]
        return uns_node_ids, relationships

    @staticmethod
    def _normalize_relationships(
        relationships: list[dict[str, Any]],
        datasource_by_node_id: dict[str, int],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for relationship in relationships:
            source = str(
                relationship.get('source_id')
                or relationship.get('source')
                or ''
            ).strip()
            target = str(
                relationship.get('target_id')
                or relationship.get('target')
                or ''
            ).strip()
            source_id = int(datasource_by_node_id.get(source) or 0)
            target_id = int(datasource_by_node_id.get(target) or 0)
            if source_id <= 0 or target_id <= 0 or source_id == target_id:
                continue
            matches = relationship.get('field_matches') or []
            if not isinstance(matches, list):
                continue
            source_fields: list[str] = []
            target_fields: list[str] = []
            for match in matches:
                if not isinstance(match, dict):
                    continue
                source_field = str(match.get('source_field') or '').strip()
                target_field = str(match.get('target_field') or '').strip()
                if source_field and target_field:
                    source_fields.append(source_field)
                    target_fields.append(target_field)
            if not source_fields:
                continue
            identity = (
                source_id,
                target_id,
                tuple(source_fields),
                tuple(target_fields),
            )
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append({
                'source_datasource_id': source_id,
                'target_datasource_id': target_id,
                'source_fields': source_fields,
                'target_fields': target_fields,
                'description': str(relationship.get('description') or '').strip(),
                'source': 'semantic_center',
                'verification_status': 'provider_asserted',
            })
        return normalized
