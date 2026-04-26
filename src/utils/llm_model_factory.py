import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen

from config import Config


@dataclass(frozen=True)
class ModelProvider:
    name: str
    adapter: str
    aliases: tuple[str, ...]
    api_key_attr: str = 'API_KEY'


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
        with urlopen(request, timeout=Config.SUPOS_REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        raise RuntimeError(f'Failed to query supos LLM gateway models: HTTP {exc.code}') from exc
    except URLError as exc:
        raise RuntimeError(f'Failed to query supos LLM gateway models: {exc.reason}') from exc

    models = payload.get('data')
    if not isinstance(models, list) or not models:
        raise ValueError('Supos LLM gateway models response must contain a non-empty data array')

    model_id = models[0].get('id') if isinstance(models[0], dict) else None
    if not model_id:
        raise ValueError('Supos LLM gateway first model entry must contain id')
    return model_id


def _resolve_model_name(provider: ModelProvider, api_key: str) -> str:
    if provider.name == 'supos_llm_gateway':
        return _resolve_supos_gateway_model(Config.BASE_URL, api_key)
    return Config.MODEL


def create_data_insight_model():
    """Create the currently configured chat model used by the insight agent."""
    provider = resolve_model_provider(Config.LLM_PROVIDER)
    api_key = getattr(Config, provider.api_key_attr)
    model_name = _resolve_model_name(provider, api_key)
    if provider.adapter == 'openai':
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=Config.BASE_URL,
            temperature=Config.TEMPERATURE,
        )
    if provider.adapter == 'qwen':
        return ChatQwen(
            model=model_name,
            api_key=api_key,
            base_url=Config.BASE_URL,
        )

    raise ValueError(f"Unsupported LLM adapter='{provider.adapter}' for provider '{provider.name}'")
