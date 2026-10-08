import hashlib
import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from model import InsightNsArtifact, InsightNsExecution, InsightNsExecutionEvidence, InsightNsTurn
from config import Config
from service.post_analysis_evidence_service import (
    PostAnalysisEvidenceCompiler,
    build_execution_snapshot,
)
from utils.datasource_utils import dump_json, safe_json_loads
from utils import logger


LEGACY_EVIDENCE_PAYLOAD_FIELDS = {
    "evidence_definitions",
    "chart_trace_refs",
    "table_trace_refs",
    "report_trace_refs",
    "table_trace_metadata",
}


class EvidenceAssemblyService:
    """Assemble runtime evidence drafts into persisted execution evidence."""

    def __init__(self, session: Session):
        self.session = session

    def enqueue_for_execution(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        execution: InsightNsExecution | None,
    ) -> None:
        """Persist a durable marker so Evidence compilation can run after the response."""
        if (
            not Config.POST_ANALYSIS_EVIDENCE_ENABLED
            or execution is None
            or int(getattr(execution, 'id', 0) or 0) <= 0
            or str(getattr(execution, 'execution_status', '') or '') != 'success'
            or not str(getattr(execution, 'generated_code', '') or '').strip()
        ):
            return
        execution_id = int(execution.id)
        row = self.session.query(InsightNsExecutionEvidence).filter(
            InsightNsExecutionEvidence.execution_id == execution_id,
        ).first()
        now = datetime.now()
        if row is None:
            row = InsightNsExecutionEvidence(
                conversation_id=conversation_id,
                turn_id=turn_id,
                execution_id=execution_id,
                schema_version='v2',
                compilation_status='queued',
                diagnostics_json=dump_json({}),
                query_snapshots_json=dump_json({}),
                evidence_map_json=dump_json({}),
                is_deleted=0,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
            return
        row.conversation_id = conversation_id
        row.turn_id = turn_id
        row.schema_version = 'v2'
        row.compilation_status = 'queued'
        row.diagnostics_json = dump_json({})
        row.query_snapshots_json = dump_json({})
        row.evidence_map_json = dump_json({})
        row.is_deleted = 0
        row.updated_at = now

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
        if str(getattr(execution, 'execution_status', '') or '') != 'success':
            logger.warning(
                'skip post-analysis evidence for non-successful execution: execution_id=%s status=%s',
                int(getattr(execution, 'id', 0) or 0),
                str(getattr(execution, 'execution_status', '') or ''),
            )
            return

        payload = safe_json_loads(getattr(execution, 'result_payload_json', '') or '{}', {})
        if not isinstance(payload, dict):
            return

        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        generated_code = str(getattr(execution, 'generated_code', '') or '').strip()
        post_compilation_attempted = False
        compilation_status = "complete"
        compilation_diagnostics: dict[str, Any] = {}
        compiled_bundle: dict[str, Any] = {}
        if Config.POST_ANALYSIS_EVIDENCE_ENABLED and (generated_code or payload.get('runtime_evidence')):
            post_compilation_attempted = True
            selected_snapshot = safe_json_loads(
                getattr(turn, 'selected_datasource_snapshot_json', '') or '[]',
                [],
            ) if turn is not None else []
            try:
                snapshot_started_at = time.perf_counter()
                snapshot = build_execution_snapshot(
                    execution=execution,
                    artifacts=artifacts,
                    selected_datasource_snapshot=(
                        selected_snapshot if isinstance(selected_snapshot, list) else []
                    ),
                    user_query=getattr(turn, 'user_query', '') if turn is not None else '',
                )
                compiler_started_at = time.perf_counter()
                if payload.get('runtime_evidence'):
                    from analysis_runtime.runtime_evidence import compile_runtime_evidence
                    compiled_bundle = compile_runtime_evidence(payload['runtime_evidence'],
                        traces=payload.get('semantic_execution_traces') or [],
                        queries=payload.get('query_snapshots') or {}, tables=payload.get('tables') or [],
                        charts=payload.get('charts') or [], report=payload.get('analysis_report') or '')
                else:
                    compiled_bundle = PostAnalysisEvidenceCompiler().compile(snapshot)
                compiler_elapsed = time.perf_counter() - compiler_started_at
                compilation_status = str(compiled_bundle.get("status") or "partial")
                compilation_diagnostics = (
                    compiled_bundle.get("diagnostics")
                    if isinstance(compiled_bundle.get("diagnostics"), dict)
                    else {}
                )
                logger.info(
                    "post-analysis evidence compiled: execution_id=%s status=%s definitions=%s chart_refs=%s table_refs=%s report_refs=%s snapshot_elapsed=%.2fs compiler_elapsed=%.2fs total_elapsed=%.2fs",
                    int(getattr(execution, 'id', 0) or 0),
                    compiled_bundle.get('status'),
                    len(compiled_bundle.get('evidence_definitions') or []),
                    len(compiled_bundle.get('chart_trace_refs') or []),
                    len(compiled_bundle.get('table_trace_refs') or []),
                    len(compiled_bundle.get('report_trace_refs') or []),
                    compiler_started_at - snapshot_started_at,
                    compiler_elapsed,
                    time.perf_counter() - snapshot_started_at,
                )
            except Exception as exc:
                # Evidence is post-processing. It must never invalidate an already
                # successful analysis execution or suppress its artifacts.
                logger.warning(
                    "post-analysis evidence unavailable: execution_id=%s error=%s",
                    int(getattr(execution, 'id', 0) or 0),
                    exc,
                    exc_info=True,
                )
                compilation_status = "failed"
                compilation_diagnostics = {"reason": "unexpected_compiler_failure", "error": str(exc)[:1200]}

        evidence_definitions = compiled_bundle.get("evidence_definitions") or []
        chart_trace_refs = compiled_bundle.get("chart_trace_refs") or []
        table_trace_refs = compiled_bundle.get("table_trace_refs") or []
        report_trace_refs = compiled_bundle.get("report_trace_refs") or []
        query_snapshots = compiled_bundle.get("query_snapshots") or payload.get("query_snapshots") or {}

        self._clear_artifact_evidence_metadata(artifacts)
        if not evidence_definitions and not chart_trace_refs and not table_trace_refs and not report_trace_refs:
            if post_compilation_attempted:
                self._upsert_execution_evidence(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    execution_id=int(execution.id or 0),
                    query_snapshots=query_snapshots if isinstance(query_snapshots, dict) else {},
                    evidence_map={},
                    compilation_status=compilation_status,
                    diagnostics=compilation_diagnostics,
                )
            else:
                self._delete_execution_evidence(int(execution.id or 0))
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
            compilation_status=compilation_status,
            diagnostics=compilation_diagnostics,
        )
        self._write_artifact_metadata(
            artifact_refs=artifact_refs,
        )
        self._cleanup_execution_payload(execution, payload)

    def _delete_execution_evidence(self, execution_id: int) -> None:
        if execution_id <= 0:
            return
        row = self.session.query(InsightNsExecutionEvidence).filter(
            InsightNsExecutionEvidence.execution_id == execution_id,
        ).first()
        if row is not None:
            self.session.delete(row)

    def _clear_artifact_evidence_metadata(self, artifacts: list[dict[str, Any]]) -> None:
        artifact_ids = [
            int(artifact.get("id") or 0)
            for artifact in artifacts
            if int(artifact.get("id") or 0) > 0
        ]
        if not artifact_ids:
            return
        rows = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.id.in_(artifact_ids),
            InsightNsArtifact.is_deleted == 0,
        ).all()
        for artifact in rows:
            metadata = safe_json_loads(artifact.metadata_json, {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.pop("evidence_refs", None)
            metadata.pop("evidence_meta", None)
            artifact.metadata_json = dump_json(metadata)

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
        for artifact in sorted(
            artifacts,
            key=lambda item: (
                str(item.get("artifact_type") or ""),
                int(item.get("sort_no") or 0),
                int(item.get("id") or 0),
            ),
        ):
            artifact_type = str(artifact.get("artifact_type") or '').strip()
            if not artifact_type:
                continue
            artifact_id = int(artifact.get("id") or 0)
            if artifact_id > 0:
                lookup[("id", artifact_id)] = artifact
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
            artifact_id = int(ref.get("artifact_id") or 0)
            artifact = artifact_lookup.get(("id", artifact_id)) if artifact_id > 0 else None
            if artifact is None:
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
        compilation_status: str = "complete",
        diagnostics: dict[str, Any] | None = None,
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
                schema_version='v2',
                compilation_status=compilation_status,
                diagnostics_json=dump_json(diagnostics or {}),
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
        row.schema_version = 'v2'
        row.compilation_status = compilation_status
        row.diagnostics_json = dump_json(diagnostics or {})
        row.query_snapshots_json = dump_json(query_snapshots)
        row.evidence_map_json = dump_json(evidence_map)
        row.is_deleted = 0
        row.updated_at = now

    def _write_artifact_metadata(
        self,
        *,
        artifact_refs: dict[int, dict[str, list[dict[str, Any]]]],
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

        evidence_query_keys = {
            str(item).strip()
            for item in (evidence_definition or {}).get("query_refs") or []
            if str(item).strip()
        }
        identifier_to_query_keys: dict[str, list[str]] = {}
        owner_to_query_keys: dict[int, list[str]] = {}
        for query_key, snapshot in (query_snapshots or {}).items():
            query_key_text = str(query_key)
            if evidence_query_keys and query_key_text not in evidence_query_keys:
                continue
            implementation = ((snapshot or {}).get("implementation") or {})
            owner = implementation.get('datasource_id')
            if owner is not None:
                owner_to_query_keys.setdefault(int(owner), []).append(query_key_text)
            sql_context = implementation.get("sql_context") or {}
            fragments = sql_context.get("fragments") or []
            for fragment in fragments if isinstance(fragments, list) else []:
                if not isinstance(fragment, dict):
                    continue
                identifier = str(
                    fragment.get("datasource_identifier")
                    or fragment.get("ts_query_source")
                    or fragment.get("table_name")
                    or ''
                ).strip()
                if identifier:
                    identifier_to_query_keys.setdefault(identifier, []).append(query_key_text)
            sql_text = str(implementation.get("sql") or '')
            if sql_text:
                for row in snapshot_rows:
                    if not isinstance(row, dict):
                        continue
                    identifier = str(row.get("datasource_identifier") or row.get("table_name") or '').strip()
                    if identifier and re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
                        sql_text,
                        re.IGNORECASE,
                    ):
                        identifier_to_query_keys.setdefault(identifier, []).append(query_key_text)

        refs: list[dict[str, Any]] = []
        has_precise_source = bool(identifier_to_query_keys or owner_to_query_keys)
        for row in snapshot_rows:
            if not isinstance(row, dict):
                continue
            datasource_id = int(row.get("datasource_id") or row.get("id") or 0)
            identifier = str(row.get("datasource_identifier") or row.get("table_name") or '').strip()
            query_keys = list(dict.fromkeys(owner_to_query_keys.get(datasource_id, [])
                                           + identifier_to_query_keys.get(identifier, [])))
            if has_precise_source and not query_keys:
                continue
            if not has_precise_source and len(used_ids) != 1:
                continue
            if used_ids and datasource_id not in used_ids:
                continue
            ref = {
                "datasource_id": datasource_id or None,
                "datasource_name": row.get("datasource_name") or '',
                "datasource_type": row.get("datasource_type") or '',
                "datasource_identifier": identifier,
                "resolution": "query_scope" if query_keys else "execution_scope",
                "query_keys": query_keys or sorted(evidence_query_keys),
            }
            field_summary = self._build_source_field_summary(
                row.get("metadata_schema") if isinstance(row.get("metadata_schema"), dict) else {},
                evidence_definition or {},
            )
            ref.update(field_summary)
            refs.append(ref)
        if refs:
            return refs
        if len(used_ids) == 1:
            return [{
                "datasource_id": next(iter(used_ids)),
                "resolution": "execution_scope",
                "query_keys": sorted(evidence_query_keys),
            }]
        return [{
            "datasource_id": None,
            "resolution": "query_scope",
            "query_keys": sorted(evidence_query_keys),
        }]

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
    def _cleanup_execution_payload(execution: InsightNsExecution, payload: dict[str, Any]) -> None:
        cleaned = dict(payload)
        for field in LEGACY_EVIDENCE_PAYLOAD_FIELDS:
            cleaned.pop(field, None)
        if cleaned != payload:
            execution.result_payload_json = dump_json(cleaned)
