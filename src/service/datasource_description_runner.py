from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime
from typing import Any

from config import Config
from config.database import SessionLocal
from model import InsightDatasource
from utils import dump_json, logger, safe_json_loads
from utils.context_compression import clip_text
from utils.llm_model_factory import create_data_insight_model, create_runtime_config


DESCRIPTION_SOURCE_DEFAULT = 'system_default'
DESCRIPTION_SOURCE_LLM = 'llm'
DESCRIPTION_SOURCE_USER = 'user'


class DatasourceDescriptionRunner:
    """Generate uploaded datasource descriptions without blocking upload for long."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(Config, 'DATASOURCE_DESCRIPTION_MAX_WORKERS', 1) or 1))
        )

    def submit(self, items: list[dict[str, Any]], selected_model_id: str = '') -> Future:
        return self._executor.submit(
            self._run,
            [dict(item) for item in items if item],
            (selected_model_id or '').strip(),
        )

    def _run(self, items: list[dict[str, Any]], selected_model_id: str = '') -> dict[int, str]:
        if not items:
            return {}
        try:
            descriptions = _generate_descriptions(items, selected_model_id=selected_model_id)
        except Exception:
            logger.warning("LLM 生成上传数据源描述失败，保留默认描述", exc_info=True)
            return {}

        if descriptions:
            applied_count = _apply_descriptions(items, descriptions)
            logger.info(
                "上传数据源 LLM 描述生成完成: generated=%s applied=%s datasource_ids=%s",
                len(descriptions),
                applied_count,
                sorted(descriptions.keys()),
            )
        else:
            logger.warning(
                "LLM 未返回可用的数据源描述，保留默认描述: datasource_ids=%s",
                [int(item.get('datasource_id') or 0) for item in items],
            )
        return descriptions


def build_description_meta(source: str, version: int = 1) -> dict[str, Any]:
    return {
        "source": source,
        "version": version,
        "updated_at": datetime.now().isoformat(),
    }


def schema_guard_hash(schema_payload: dict[str, Any]) -> str:
    comparable = dict(schema_payload or {})
    comparable.pop('description', None)
    raw = json.dumps(comparable, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _generate_descriptions(items: list[dict[str, Any]], selected_model_id: str = '') -> dict[int, str]:
    prompt = _build_prompt(items)
    runtime_config = create_runtime_config(selected_model_id=selected_model_id)
    model = create_data_insight_model(runtime_config)
    response = _invoke_model_with_timeout(model, prompt)
    content = _extract_content(response)
    payload = _parse_json_object(content)
    rows = payload.get('descriptions') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}

    result: dict[int, str] = {}
    valid_ids = {int(item.get('datasource_id') or 0) for item in items}
    for row in rows:
        if not isinstance(row, dict):
            continue
        datasource_id = int(row.get('datasource_id') or 0)
        description = _clean_description(row.get('description'))
        if datasource_id in valid_ids and description:
            result[datasource_id] = description
    return result


def _invoke_model_with_timeout(model: Any, prompt: str) -> Any:
    timeout_seconds = max(float(getattr(Config, 'DATASOURCE_DESCRIPTION_LLM_TIMEOUT_SECONDS', 15) or 0), 0)
    if timeout_seconds <= 0:
        return model.invoke(prompt)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(model.invoke, prompt)
    try:
        return future.result(timeout=timeout_seconds)
    except TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"datasource description LLM timed out after {timeout_seconds:g}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _build_prompt(items: list[dict[str, Any]]) -> str:
    slim_items = []
    for item in items:
        preview = dict(item.get('preview_summary') or {})
        slim_items.append({
            "datasource_id": item.get('datasource_id'),
            "datasource_name": item.get('datasource_name'),
            "original_filename": item.get('original_filename'),
            "sheet_name": item.get('sheet_name'),
            "columns": preview.get('columns') or [],
            "sample_rows": preview.get('sample_rows') or [],
            "column_stats": preview.get('column_stats') or {},
            "row_count_preview": preview.get('row_count_preview'),
        })

    return (
        "你是数据分析产品的数据源语义描述助手。请根据真实样本数据和字段统计，为每个上传数据源生成一段中文描述。\n"
        "要求：\n"
        "1. 只输出 JSON，不要输出 Markdown 或解释。\n"
        "2. descriptions 数组中必须逐项返回 datasource_id 和 description。\n"
        "3. description 用 1 到 2 句话说明该数据源大致记录什么对象、包含哪些关键信息、适合做什么分析。\n"
        "4. 不要编造样本中不存在的业务事实；无法判断业务含义时用谨慎表述。\n"
        "5. 不要泄露整行样本数据，不要逐字段罗列。\n\n"
        "输出格式：{\"descriptions\":[{\"datasource_id\":1,\"description\":\"...\"}]}\n\n"
        f"数据源样本摘要：\n{clip_text(json.dumps(slim_items, ensure_ascii=False, default=str), 12000)}"
    )


def _extract_content(response: Any) -> str:
    content = getattr(response, 'content', response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get('text') or item.get('content') or ''))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()
    return str(content or '').strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    normalized = (text or '').strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
        if isinstance(payload, dict) and isinstance(payload.get('descriptions'), list):
            return payload
    except Exception:
        pass

    fence_matches = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', normalized, flags=re.S | re.I)
    for candidate in reversed(fence_matches):
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict) and isinstance(payload.get('descriptions'), list):
                return payload
        except Exception:
            continue

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(normalized):
        if char != '{':
            continue
        try:
            payload, _end = decoder.raw_decode(normalized[index:])
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get('descriptions'), list):
            candidates.append(payload)
    return candidates[-1] if candidates else {}


def _clean_description(value: Any) -> str:
    text = " ".join(str(value or '').split())
    if not text:
        return ''
    return clip_text(text, int(getattr(Config, 'DATASOURCE_DESCRIPTION_MAX_CHARS', 500) or 500))


def _apply_descriptions(items: list[dict[str, Any]], descriptions: dict[int, str]) -> int:
    session = SessionLocal()
    applied_count = 0
    try:
        item_map = {int(item.get('datasource_id') or 0): item for item in items}
        for datasource_id, description in descriptions.items():
            item = item_map.get(datasource_id)
            if not item or not description:
                continue
            datasource = session.query(InsightDatasource).filter(
                InsightDatasource.id == datasource_id,
                InsightDatasource.is_deleted == 0,
            ).first()
            if datasource is None:
                logger.info("跳过写回数据源描述，数据源不存在: datasource_id=%s", datasource_id)
                continue
            schema_payload = safe_json_loads(datasource.datasource_schema, {})
            if not isinstance(schema_payload, dict):
                logger.info("跳过写回数据源描述，schema 无效: datasource_id=%s", datasource_id)
                continue
            config_payload = safe_json_loads(datasource.datasource_config_json, {})
            if not isinstance(config_payload, dict):
                config_payload = {}
            current_meta = config_payload.get('description_meta') if isinstance(config_payload.get('description_meta'), dict) else {}
            if current_meta.get('source') == DESCRIPTION_SOURCE_USER:
                logger.info("跳过写回数据源描述，用户已手动编辑: datasource_id=%s", datasource_id)
                continue
            if schema_guard_hash(schema_payload) != item.get('schema_hash'):
                logger.info("跳过写回数据源描述，schema 已变化: datasource_id=%s", datasource_id)
                continue
            if str(schema_payload.get('description') or '').strip() != str(item.get('base_description') or '').strip():
                logger.info("跳过写回数据源描述，描述已变化: datasource_id=%s", datasource_id)
                continue
            schema_payload['description'] = description
            config_payload['description_meta'] = build_description_meta(
                DESCRIPTION_SOURCE_LLM,
                int(current_meta.get('version') or 1) + 1,
            )
            datasource.datasource_schema = dump_json(schema_payload)
            datasource.datasource_config_json = dump_json(config_payload)
            applied_count += 1
        session.commit()
        return applied_count
    except Exception:
        session.rollback()
        logger.warning("写回上传数据源 LLM 描述失败", exc_info=True)
        return applied_count
    finally:
        session.close()


_runner: DatasourceDescriptionRunner | None = None


def get_datasource_description_runner() -> DatasourceDescriptionRunner:
    global _runner
    if _runner is None:
        _runner = DatasourceDescriptionRunner()
    return _runner
