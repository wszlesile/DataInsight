import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from model import InsightNsArtifact, InsightNsExecution, InsightNsExecutionEvidence, InsightNsTurn
from utils.datasource_utils import dump_json, safe_json_loads


EVIDENCE_PAYLOAD_FIELDS = {
    "evidence_definitions",
    "chart_trace_refs",
    "table_trace_refs",
    "report_trace_refs",
    "table_trace_metadata",
    "query_snapshots",
}


class EvidenceAssemblyService:
    """Assemble runtime evidence drafts into persisted execution evidence."""

    def __init__(self, session: Session):
        self.session = session

    def assemble_for_execution(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        execution: InsightNsExecution | None,
        artifacts: list[dict[str, Any]],
    ) -> None:
        if execution is None:
            return

        payload = safe_json_loads(getattr(execution, 'result_payload_json', '') or '{}', {})
        if not isinstance(payload, dict):
            return

        evidence_definitions = payload.get("evidence_definitions") or []
        chart_trace_refs = payload.get("chart_trace_refs") or []
        table_trace_refs = payload.get("table_trace_refs") or []
        report_trace_refs = payload.get("report_trace_refs") or []
        query_snapshots = payload.get("query_snapshots") or {}
        table_trace_metadata = payload.get("table_trace_metadata") or {}

        if not evidence_definitions and not chart_trace_refs and not table_trace_refs and not report_trace_refs:
            self._cleanup_execution_payload(execution, payload)
            return

        evidence_key_to_id: dict[str, str] = {}
        evidence_map: dict[str, Any] = {}
        for item in evidence_definitions:
            if not isinstance(item, dict):
                continue
            evidence_key = str(item.get("evidence_key") or '').strip()
            if not evidence_key:
                continue
            evidence_id = self._build_evidence_id(int(execution.id or 0), evidence_key)
            evidence_key_to_id[evidence_key] = evidence_id
            evidence_item = dict(item)
            evidence_item.pop("evidence_key", None)
            evidence_item["evidence_id"] = evidence_id
            evidence_item["execution_ref"] = {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "execution_id": int(execution.id or 0),
                "analysis_task_id": int(getattr(execution, 'analysis_task_id', 0) or 0) or None,
            }
            evidence_item["source_refs"] = self._build_source_refs(
                turn_id=turn_id,
                execution=execution,
                query_snapshots=query_snapshots if isinstance(query_snapshots, dict) else {},
                evidence_definition=evidence_item,
            )
            evidence_item["scopes"] = []
            evidence_map[evidence_id] = evidence_item

        artifact_lookup = self._build_artifact_lookup(artifacts)
        artifact_refs: dict[int, dict[str, list[dict[str, Any]]]] = {}

        self._attach_refs(
            refs=chart_trace_refs,
            group_name="chart_trace_refs",
            artifact_type="chart",
            index_field="chart_index",
            artifact_lookup=artifact_lookup,
            evidence_key_to_id=evidence_key_to_id,
            artifact_refs=artifact_refs,
            evidence_map=evidence_map,
        )
        self._attach_refs(
            refs=table_trace_refs,
            group_name="table_trace_refs",
            artifact_type="table",
            index_field="table_index",
            artifact_lookup=artifact_lookup,
            evidence_key_to_id=evidence_key_to_id,
            artifact_refs=artifact_refs,
            evidence_map=evidence_map,
        )
        self._attach_refs(
            refs=report_trace_refs,
            group_name="report_trace_refs",
            artifact_type="report",
            index_field="report_index",
            artifact_lookup=artifact_lookup,
            evidence_key_to_id=evidence_key_to_id,
            artifact_refs=artifact_refs,
            evidence_map=evidence_map,
        )

        self._upsert_execution_evidence(
            conversation_id=conversation_id,
            turn_id=turn_id,
            execution_id=int(execution.id or 0),
            query_snapshots=query_snapshots if isinstance(query_snapshots, dict) else {},
            evidence_map=evidence_map,
        )
        self._write_artifact_metadata(
            artifact_refs=artifact_refs,
            table_trace_metadata=table_trace_metadata if isinstance(table_trace_metadata, dict) else {},
        )
        self._cleanup_execution_payload(execution, payload)

    @staticmethod
    def _build_evidence_id(execution_id: int, evidence_key: str) -> str:
        digest = hashlib.sha256(f"{execution_id}:{evidence_key}".encode('utf-8')).hexdigest()[:12]
        return f"ev_{execution_id}_{digest}"

    @staticmethod
    def _build_ref_id(execution_id: int, artifact_id: int, group_name: str, index: int) -> str:
        digest = hashlib.sha256(f"{execution_id}:{artifact_id}:{group_name}:{index}".encode('utf-8')).hexdigest()[:10]
        return f"ref_{digest}"

    @staticmethod
    def _build_artifact_lookup(artifacts: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
        lookup: dict[tuple[str, int], dict[str, Any]] = {}
        counters: dict[str, int] = {}
        for artifact in artifacts:
            artifact_type = str(artifact.get("artifact_type") or '').strip()
            if not artifact_type:
                continue
            index = counters.get(artifact_type, 0)
            counters[artifact_type] = index + 1
            lookup[(artifact_type, index)] = artifact
        return lookup

    def _attach_refs(
        self,
        *,
        refs: Any,
        group_name: str,
        artifact_type: str,
        index_field: str,
        artifact_lookup: dict[tuple[str, int], dict[str, Any]],
        evidence_key_to_id: dict[str, str],
        artifact_refs: dict[int, dict[str, list[dict[str, Any]]]],
        evidence_map: dict[str, Any],
    ) -> None:
        if not isinstance(refs, list):
            return
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                continue
            evidence_id = evidence_key_to_id.get(str(ref.get("evidence_key") or '').strip())
            if not evidence_id:
                continue
            artifact_index = int(ref.get(index_field) or 0)
            artifact = artifact_lookup.get((artifact_type, artifact_index))
            if artifact is None and artifact_type == "report":
                artifact = artifact_lookup.get((artifact_type, 0))
            if artifact is None:
                continue
            artifact_id = int(artifact.get("id") or 0)
            if artifact_id <= 0:
                continue
            resolved_ref = dict(ref)
            resolved_ref.pop("evidence_key", None)
            resolved_ref["ref_id"] = self._build_ref_id(int(artifact.get("execution_id") or 0), artifact_id, group_name, index)
            resolved_ref["evidence_id"] = evidence_id
            resolved_ref["artifact_id"] = artifact_id
            resolved_ref["artifact_type"] = artifact_type
            artifact_refs.setdefault(artifact_id, {}).setdefault(group_name, []).append(resolved_ref)
            evidence_item = evidence_map.get(evidence_id)
            if isinstance(evidence_item, dict):
                evidence_item.setdefault("scopes", []).append({
                    "artifact_id": artifact_id,
                    "artifact_type": artifact_type,
                    "ref_id": resolved_ref["ref_id"],
                })

    def _upsert_execution_evidence(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        execution_id: int,
        query_snapshots: dict[str, Any],
        evidence_map: dict[str, Any],
    ) -> None:
        if execution_id <= 0:
            return
        row = self.session.query(InsightNsExecutionEvidence).filter(
            InsightNsExecutionEvidence.execution_id == execution_id,
        ).first()
        now = datetime.now()
        if row is None:
            row = InsightNsExecutionEvidence(
                conversation_id=conversation_id,
                turn_id=turn_id,
                execution_id=execution_id,
                schema_version='v1',
                query_snapshots_json=dump_json(query_snapshots),
                evidence_map_json=dump_json(evidence_map),
                is_deleted=0,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
            return
        row.conversation_id = conversation_id
        row.turn_id = turn_id
        row.query_snapshots_json = dump_json(query_snapshots)
        row.evidence_map_json = dump_json(evidence_map)
        row.is_deleted = 0
        row.updated_at = now

    def _write_artifact_metadata(
        self,
        *,
        artifact_refs: dict[int, dict[str, list[dict[str, Any]]]],
        table_trace_metadata: dict[str, Any],
    ) -> None:
        if not artifact_refs:
            return
        rows = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.id.in_(list(artifact_refs.keys())),
            InsightNsArtifact.is_deleted == 0,
        ).all()
        for artifact in rows:
            metadata = safe_json_loads(artifact.metadata_json, {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["evidence_refs"] = artifact_refs.get(int(artifact.id or 0), {})
            if artifact.artifact_type == "table" and table_trace_metadata:
                metadata["evidence_meta"] = self._metadata_for_table_artifact(artifact, table_trace_metadata)
            artifact.metadata_json = dump_json(metadata)

    def _build_source_refs(
        self,
        *,
        turn_id: int,
        execution: InsightNsExecution,
        query_snapshots: dict[str, Any],
        evidence_definition: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        used_ids = {
            int(item) for item in safe_json_loads(getattr(execution, 'used_datasource_ids_json', '') or '[]', [])
            if str(item).strip().isdigit()
        }
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        snapshot_rows = []
        if turn is not None:
            snapshot_rows = safe_json_loads(getattr(turn, 'selected_datasource_snapshot_json', '') or '[]', [])
        if not isinstance(snapshot_rows, list):
            snapshot_rows = []

        identifier_to_query_keys: dict[str, list[str]] = {}
        for query_key, snapshot in (query_snapshots or {}).items():
            implementation = ((snapshot or {}).get("implementation") or {})
            sql_context = implementation.get("sql_context") or {}
            fragments = sql_context.get("fragments") or []
            if not isinstance(fragments, list):
                continue
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                identifier = str(
                    fragment.get("datasource_identifier")
                    or fragment.get("ts_query_source")
                    or fragment.get("table_name")
                    or ''
                ).strip()
                if identifier:
                    identifier_to_query_keys.setdefault(identifier, []).append(str(query_key))
            sql_text = str(implementation.get("sql") or '')
            if sql_text:
                for row in snapshot_rows:
                    if not isinstance(row, dict):
                        continue
                    identifier = str(row.get("datasource_identifier") or row.get("table_name") or '').strip()
                    if identifier and identifier in sql_text:
                        identifier_to_query_keys.setdefault(identifier, []).append(str(query_key))

        refs: list[dict[str, Any]] = []
        for row in snapshot_rows:
            if not isinstance(row, dict):
                continue
            datasource_id = int(row.get("datasource_id") or row.get("id") or 0)
            identifier = str(row.get("datasource_identifier") or row.get("table_name") or '').strip()
            if used_ids and datasource_id not in used_ids and identifier not in identifier_to_query_keys:
                continue
            ref = {
                "datasource_id": datasource_id or None,
                "datasource_name": row.get("datasource_name") or '',
                "datasource_type": row.get("datasource_type") or '',
                "datasource_identifier": identifier,
                "resolution": "query_scope" if identifier in identifier_to_query_keys else "execution_scope",
                "query_keys": identifier_to_query_keys.get(identifier, []),
            }
            field_summary = self._build_source_field_summary(
                row.get("metadata_schema") if isinstance(row.get("metadata_schema"), dict) else {},
                evidence_definition or {},
            )
            ref.update(field_summary)
            refs.append(ref)
        if refs:
            return refs
        return [{"datasource_id": datasource_id, "resolution": "execution_scope"} for datasource_id in sorted(used_ids)]

    @staticmethod
    def _build_source_field_summary(metadata_schema: dict[str, Any], evidence_definition: dict[str, Any]) -> dict[str, Any]:
        field_names = EvidenceAssemblyService._evidence_metric_field_names(evidence_definition)
        if not field_names:
            return {}
        properties = metadata_schema.get("properties") if isinstance(metadata_schema.get("properties"), dict) else {}
        fields: list[dict[str, Any]] = []
        for field_name in field_names:
            prop = properties.get(field_name) if isinstance(properties, dict) else None
            field_item: dict[str, Any] = {"name": field_name}
            if isinstance(prop, dict):
                field_type = prop.get("type") or prop.get("property_type")
                description = prop.get("description") or prop.get("name")
                if field_type:
                    field_item["type"] = field_type
                if description:
                    field_item["description"] = description
            fields.append(field_item)
        return {
            "field_names": field_names,
            "fields": fields,
        }

    @staticmethod
    def _evidence_metric_field_names(evidence_definition: dict[str, Any]) -> list[str]:
        calculation = evidence_definition.get("calculation") if isinstance(evidence_definition.get("calculation"), dict) else {}
        raw_fields = calculation.get("metric_fields") or calculation.get("fields") or []
        if isinstance(raw_fields, str):
            raw_fields = [raw_fields]
        if not isinstance(raw_fields, list):
            return []
        field_names: list[str] = []
        seen: set[str] = set()
        for item in raw_fields:
            value = str(item or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            field_names.append(value)
        return field_names

    @staticmethod
    def _metadata_for_table_artifact(artifact: InsightNsArtifact, table_trace_metadata: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(table_trace_metadata, dict):
            return {}
        if isinstance(table_trace_metadata.get("tables"), list):
            # Table artifact sort_no starts at 101, so table_index is sort_no - 101.
            table_index = max(int(artifact.sort_no or 100) - 101, 0)
            rows = table_trace_metadata.get("tables") or []
            if 0 <= table_index < len(rows) and isinstance(rows[table_index], dict):
                return rows[table_index]
        if isinstance(table_trace_metadata.get("by_table"), dict):
            table_index = max(int(artifact.sort_no or 100) - 101, 0)
            value = table_trace_metadata.get("by_table", {}).get(str(table_index))
            if isinstance(value, dict):
                return value
        return table_trace_metadata

    @staticmethod
    def _cleanup_execution_payload(execution: InsightNsExecution, payload: dict[str, Any]) -> None:
        cleaned = dict(payload)
        for field in EVIDENCE_PAYLOAD_FIELDS:
            cleaned.pop(field, None)
        if cleaned != payload:
            execution.result_payload_json = dump_json(cleaned)
