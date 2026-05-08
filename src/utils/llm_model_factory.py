import json
import socket
from contextvars import ContextVar, Token
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen

from config import Config
from utils.llm_error_utils import LLMProviderUnavailableError

_CURRENT_SELECTED_MODEL_ID: ContextVar[str] = ContextVar('current_selected_llm_model_id', default='')


@dataclass(frozen=True)
class ModelProvider:
    name: str
    adapter: str
    aliases: tuple[str, ...]
    api_key_attr: str = 'API_KEY'


@dataclass(frozen=True)
class LlmRuntimeConfig:
    provider: str
    model_id: str
    base_url: str
    api_key: str
    temperature: float
    adapter: str

    @property
    def cache_key(self) -> str:
        return f"{self.provider}|{self.model_id}|{self.base_url}|{self.temperature}"


MODEL_PROVIDERS: tuple[ModelProvider, ...] = (
    ModelProvider(
        name='minimax',
        adapter='openai',
        aliases=('minimax', 'minimax-m2.5', 'minimax-m2_5', 'minimax-m2.7', 'minimax-m2.7-highspeed'),
    ),
    ModelProvider(
        name='qwen',
        adapter='qwen',
        aliases=('qwen', 'qwen3', 'qwen3-80b', 'qwen-3-80b'),
    ),
    ModelProvider(
        name='deepseek',
        adapter='openai',
        aliases=('deepseek', 'deepseek-chat', 'deepseek-reasoner'),
    ),
    ModelProvider(
        name='supos_llm_gateway',
        adapter='openai',
        aliases=('supos_llm_gateway', 'supos-llm-gateway', 'supos'),
        api_key_attr='SUPOS_LLM_GATEWAY_API_KEY',
    ),
)


def _normalize_provider_name(value: str) -> str:
    return (value or '').strip().lower()


def resolve_model_provider(active_provider: str) -> ModelProvider:
    normalized = _normalize_provider_name(active_provider)
    for provider in MODEL_PROVIDERS:
        if normalized == provider.name or normalized in provider.aliases:
            return provider

    supported = ', '.join(provider.name for provider in MODEL_PROVIDERS)
    raise ValueError(
        f"Unsupported LLM_PROVIDER='{active_provider}'. Supported providers: {supported}"
    )


def _resolve_supos_gateway_model(base_url: str, api_key: str) -> str:
    if not api_key:
        raise ValueError('SUPOS_DATAINSIGHT-SERVER_APPKEY is required when LLM_PROVIDER=supos_llm_gateway')

    models_url = f"{base_url.rstrip('/')}/models"
    request = Request(
        models_url,
        headers={'Authorization': f'Bearer {api_key}'},
        method='GET',
    )
    try:
        timeout = max(float(Config.SUPOS_LLM_GATEWAY_MODEL_TIMEOUT_SECONDS), 0.1)
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        raise LLMProviderUnavailableError(f'Failed to query supos LLM gateway models: HTTP {exc.code}') from exc
    except socket.timeout as exc:
        raise LLMProviderUnavailableError(
            f'Failed to query supos LLM gateway models: timed out after {timeout:g}s'
        ) from exc
    except URLError as exc:
        raise LLMProviderUnavailableError(f'Failed to query supos LLM gateway models: {exc.reason}') from exc

    models = payload.get('data')
    if not isinstance(models, list) or not models:
        raise LLMProviderUnavailableError('Supos LLM gateway models response must contain a non-empty data array')

    model_id = models[0].get('id') if isinstance(models[0], dict) else None
    if not model_id:
        raise LLMProviderUnavailableError('Supos LLM gateway first model entry must contain id')
    return model_id


def _resolve_provider_base_url(provider: ModelProvider) -> str:
    if provider.name == 'supos_llm_gateway':
        return f"{Config.SUPOS_WEB.rstrip('/')}/os/llm-gateway/v1"
    return Config.BASE_URL


def _resolve_model_name(provider: ModelProvider, api_key: str, base_url: str) -> str:
    if provider.name == 'supos_llm_gateway':
        return _resolve_supos_gateway_model(base_url, api_key)
    return Config.MODEL


def bind_selected_model_id(model_id: str) -> Token:
    return _CURRENT_SELECTED_MODEL_ID.set((model_id or '').strip())


def reset_selected_model_id(token: Token) -> None:
    _CURRENT_SELECTED_MODEL_ID.reset(token)


def create_runtime_config(selected_model_id: str = '') -> LlmRuntimeConfig:
    """Resolve the chat model runtime config for the current request."""
    provider = resolve_model_provider(Config.LLM_PROVIDER)
    api_key = getattr(Config, provider.api_key_attr)
    base_url = _resolve_provider_base_url(provider)
    model_id = (
        (selected_model_id or '').strip()
        or _CURRENT_SELECTED_MODEL_ID.get()
        or _resolve_model_name(provider, api_key, base_url)
    )
    return LlmRuntimeConfig(
        provider=provider.name,
        model_id=model_id,
        base_url=base_url,
        api_key=api_key,
        temperature=Config.TEMPERATURE,
        adapter=provider.adapter,
    )


def create_data_insight_model(runtime_config: LlmRuntimeConfig | None = None):
    """Create the currently configured chat model used by the insight agent."""
    runtime_config = runtime_config or create_runtime_config()
    if runtime_config.adapter == 'openai':
        return ChatOpenAI(
            model=runtime_config.model_id,
            api_key=runtime_config.api_key,
            base_url=runtime_config.base_url,
            temperature=runtime_config.temperature,
        )
    if runtime_config.adapter == 'qwen':
        return ChatQwen(
            model=runtime_config.model_id,
            api_key=runtime_config.api_key,
            base_url=runtime_config.base_url,
        )

    raise ValueError(
        f"Unsupported LLM adapter='{runtime_config.adapter}' for provider '{runtime_config.provider}'"
    )
