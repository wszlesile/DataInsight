from __future__ import annotations

from collections.abc import Callable
from typing import Any

from config import Config

ELLIPSIS_MARKER = "\n...[omitted]..."


def clip_text(text: str, max_chars: int, *, keep_head_ratio: float = 0.75) -> str:
    normalized = str(text or "")
    if max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= len(ELLIPSIS_MARKER) + 8:
        return normalized[:max_chars]

    head_chars = int(max_chars * keep_head_ratio)
    head_chars = min(max(head_chars, 1), max_chars - len(ELLIPSIS_MARKER) - 1)
    tail_chars = max_chars - head_chars - len(ELLIPSIS_MARKER)
    if tail_chars <= 0:
        return normalized[:max_chars]
    return normalized[:head_chars] + ELLIPSIS_MARKER + normalized[-tail_chars:]


def build_fallback_turn_summary(turn_payload: dict[str, Any]) -> str:
    turn_no = int(turn_payload.get("turn_no") or 0)
    question = _single_line(turn_payload.get("question") or "")[:120]
    answer = _single_line(turn_payload.get("answer") or "")[:180]
    if answer:
        return f"第{turn_no}轮 用户: {question}；系统结论: {answer}"
    return f"第{turn_no}轮 用户: {question}"


def summarize_turns_incrementally(
    *,
    existing_summary: str = "",
    turn_payloads: list[dict[str, Any]] | None = None,
    max_chars: int | None = None,
    summarizer: Callable[[str], str] | None = None,
) -> str:
    payloads = turn_payloads or []
    if not payloads:
        return clip_text(existing_summary, max_chars or Config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS)

    sections: list[str] = []
    existing = (existing_summary or "").strip()
    if existing:
        sections.append(existing)
    sections.extend(build_fallback_turn_summary(item) for item in payloads)
    combined = "\n".join(section for section in sections if section).strip()
    if not combined:
        return ""

    summary = combined
    if summarizer is not None:
        try:
            candidate = (summarizer(combined) or "").strip()
            if candidate:
                summary = candidate
        except Exception:
            summary = combined

    return clip_text(summary, max_chars or Config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS)


def build_llm_turn_summarizer() -> Callable[[str], str] | None:
    if not Config.CONTEXT_COMPRESSION_USE_LLM:
        return None

    try:
        from utils.llm_model_factory import create_data_insight_model
    except Exception:
        return None

    try:
        model = create_data_insight_model()
    except Exception:
        return None

    def _summarize(text: str) -> str:
        prompt = (
            "请压缩下面的多轮对话摘要，保留用户目标、关键结论、约束和失败信息。"
            "不要展开细节，不要编造，没有信息就省略。输出简洁中文摘要。\n\n"
            f"{clip_text(text, Config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS * 2)}"
        )
        response = model.invoke(prompt)
        content = getattr(response, "content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part).strip()
        return str(content or "").strip()

    return _summarize


def _single_line(text: str) -> str:
    return " ".join(str(text or "").split())


__all__ = [
    "build_fallback_turn_summary",
    "build_llm_turn_summarizer",
    "clip_text",
    "summarize_turns_incrementally",
]
