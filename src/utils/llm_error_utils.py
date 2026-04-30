LLM_PROVIDER_UNAVAILABLE_MESSAGE = "当前模型暂无可用渠道，请切换到其他可用模型后重试。"


class LLMProviderUnavailableError(RuntimeError):
    """Raised when the configured LLM provider cannot be reached or has no usable model channel."""


_LLM_PROVIDER_MODULE_MARKERS = (
    "openai",
    "httpx",
    "httpcore",
)
_NETWORK_MODULE_MARKERS = (
    "urllib",
    "socket",
)
_NETWORK_ERROR_NAME_MARKERS = (
    "apiconnectionerror",
    "apitimeouterror",
    "connecterror",
    "connecttimeout",
    "connectionerror",
    "connectiontimeout",
    "readtimeout",
    "timeout",
    "urlerror",
)


def is_llm_provider_unavailable_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, LLMProviderUnavailableError):
            return True

        exc_type = type(current)
        module = (getattr(exc_type, "__module__", "") or "").lower()
        name = (getattr(exc_type, "__name__", "") or exc_type.__class__.__name__).lower()
        if any(marker in module for marker in _LLM_PROVIDER_MODULE_MARKERS):
            return True
        if any(marker in module for marker in _NETWORK_MODULE_MARKERS) and any(
            marker in name for marker in _NETWORK_ERROR_NAME_MARKERS
        ):
            return True

        current = current.__cause__ or current.__context__

    return False


def get_user_facing_agent_error(exc: BaseException) -> str:
    if is_llm_provider_unavailable_error(exc):
        return LLM_PROVIDER_UNAVAILABLE_MESSAGE
    return str(exc)
