from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen

from config import Config


def create_data_insight_model():
    """Create the currently configured chat model used by the insight agent."""
    if Config.LLM_MODEL_ACTIVE == 'MiniMax':
        return ChatOpenAI(
            model=Config.MINIMAX_M2_5_MODEL,
            api_key=Config.MINIMAX_M2_5_API_KEY,
            base_url=Config.MINIMAX_M2_5_BASE_URL,
            temperature=0.7,
        )

    return ChatQwen(
        model=Config.QWEN3_80B_MODEL,
        api_key=Config.QWEN3_80B_API_KEY,
        base_url=Config.QWEN3_80B_BASE_URL,
    )
