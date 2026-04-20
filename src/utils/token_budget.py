from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage

from config import Config

try:
    import tiktoken
except Exception:  # pragma: no cover - optional dependency
    tiktoken = None


def _get_encoder():
    if tiktoken is None:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - defensive fallback
        return None


def estimate_text_tokens(text: str) -> int:
    normalized = str(text or "")
    if not normalized:
        return 0

    encoder = _get_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(normalized))
        except Exception:  # pragma: no cover - defensive fallback
            pass

    # A conservative fallback that roughly tracks Chinese + code payloads.
    return max(1, (len(normalized) + 2) // 3)


def estimate_message_tokens(message: BaseMessage) -> int:
    return 4 + estimate_text_tokens(_message_text(message))


def estimate_messages_tokens(messages: list[BaseMessage]) -> int:
    if not messages:
        return 0
    return sum(estimate_message_tokens(message) for message in messages) + 2


def take_tail_messages_within_budget(messages: list[BaseMessage], token_budget: int | None) -> list[BaseMessage]:
    if token_budget is None or token_budget <= 0:
        return list(messages)

    selected: list[BaseMessage] = []
    total = 0
    for message in reversed(messages):
        cost = estimate_message_tokens(message)
        if selected and total + cost > token_budget:
            break
        if not selected and cost > token_budget:
            selected.append(message)
            break
        selected.append(message)
        total += cost
    return list(reversed(selected))


def fit_system_messages_within_budget(messages: list[BaseMessage], token_budget: int | None) -> list[BaseMessage]:
    if token_budget is None or token_budget <= 0:
        return list(messages)

    selected: list[BaseMessage] = []
    total = 0
    for message in messages:
        cost = estimate_message_tokens(message)
        if selected and total + cost > token_budget:
            break
        if not selected and cost > token_budget:
            selected.append(message)
            break
        selected.append(message)
        total += cost
    return selected


def describe_budget_split(
    *,
    max_prompt_tokens: int | None = None,
    fixed_tokens: int = 0,
    history_ratio: float | None = None,
    min_history_tokens: int | None = None,
    min_memory_tokens: int | None = None,
) -> dict[str, int]:
    prompt_budget = max(int(max_prompt_tokens or Config.CONTEXT_COMPRESSION_PROMPT_MAX_TOKENS), 0)
    remaining = max(prompt_budget - max(int(fixed_tokens), 0), 0)

    history_ratio_value = history_ratio
    if history_ratio_value is None:
        history_ratio_value = Config.CONTEXT_COMPRESSION_HISTORY_TOKEN_RATIO
    history_ratio_value = min(max(float(history_ratio_value), 0.0), 1.0)

    history_min = max(int(min_history_tokens or Config.CONTEXT_COMPRESSION_MIN_HISTORY_TOKENS), 0)
    memory_min = max(int(min_memory_tokens or Config.CONTEXT_COMPRESSION_MIN_MEMORY_TOKENS), 0)

    if remaining <= 0:
        return {
            "prompt_budget": prompt_budget,
            "fixed_tokens": max(int(fixed_tokens), 0),
            "remaining_tokens": 0,
            "history_budget": 0,
            "memory_budget": 0,
        }

    history_budget = int(remaining * history_ratio_value)
    memory_budget = remaining - history_budget

    if remaining >= history_min + memory_min:
        history_budget = max(history_budget, history_min)
        memory_budget = max(memory_budget, memory_min)
        overflow = history_budget + memory_budget - remaining
        if overflow > 0:
            if history_budget > history_min:
                reduce_from_history = min(overflow, history_budget - history_min)
                history_budget -= reduce_from_history
                overflow -= reduce_from_history
            if overflow > 0 and memory_budget > memory_min:
                memory_budget -= min(overflow, memory_budget - memory_min)
    else:
        history_budget = max(int(remaining * history_ratio_value), 0)
        memory_budget = max(remaining - history_budget, 0)

    return {
        "prompt_budget": prompt_budget,
        "fixed_tokens": max(int(fixed_tokens), 0),
        "remaining_tokens": remaining,
        "history_budget": max(history_budget, 0),
        "memory_budget": max(memory_budget, 0),
    }


def shrink_messages_to_budget(messages: list[BaseMessage], token_budget: int | None) -> list[BaseMessage]:
    if token_budget is None or token_budget <= 0:
        return list(messages)
    if estimate_messages_tokens(messages) <= token_budget:
        return list(messages)
    if not messages:
        return []

    head: list[BaseMessage] = []
    tail_start = 0
    if _is_system_message(messages[0]):
        head = [messages[0]]
        tail_start = 1

    tail_messages = messages[tail_start:]
    if not tail_messages:
        return head[:1]

    head_tokens = estimate_messages_tokens(head)
    allowed_tail_tokens = max(token_budget - head_tokens, 0)
    trimmed_tail = take_tail_messages_within_budget(tail_messages, allowed_tail_tokens)
    result = [*head, *trimmed_tail]

    if estimate_messages_tokens(result) <= token_budget:
        return result

    if head:
        return take_tail_messages_within_budget(result, token_budget)
    return trimmed_tail


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _is_system_message(message: BaseMessage) -> bool:
    return getattr(message, "type", "") == "system" or message.__class__.__name__ == "SystemMessage"


__all__ = [
    "describe_budget_split",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "fit_system_messages_within_budget",
    "shrink_messages_to_budget",
    "take_tail_messages_within_budget",
]
