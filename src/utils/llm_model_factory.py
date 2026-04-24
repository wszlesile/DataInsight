from dataclasses import dataclass

from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen

from config import Config


@dataclass(frozen=True)
class ModelProvider:
    name: str
    adapter: str
    aliases: tuple[str, ...]


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


def create_data_insight_model():
    """Create the currently configured chat model used by the insight agent."""
    provider = resolve_model_provider(Config.LLM_PROVIDER)
    if provider.adapter == 'openai':
        return ChatOpenAI(
            model=Config.MODEL,
            api_key=Config.API_KEY,
            base_url=Config.BASE_URL,
            temperature=Config.TEMPERATURE,
        )
    if provider.adapter == 'qwen':
        return ChatQwen(
            model=Config.MODEL,
            api_key=Config.API_KEY,
            base_url=Config.BASE_URL,
        )

    raise ValueError(f"Unsupported LLM adapter='{provider.adapter}' for provider '{provider.name}'")
