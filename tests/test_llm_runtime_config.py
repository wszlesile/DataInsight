import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class LlmRuntimeConfigTestCase(unittest.TestCase):
    def test_runtime_config_uses_selected_model_id(self) -> None:
        from utils.llm_model_factory import create_runtime_config

        with patch("utils.llm_model_factory.Config") as config:
            config.LLM_PROVIDER = "supos_llm_gateway"
            config.SUPOS_WEB = "http://supos.example/"
            config.SUPOS_LLM_GATEWAY_API_KEY = "app-key"
            config.TEMPERATURE = 0.2

            runtime_config = create_runtime_config(selected_model_id="model-b")

        self.assertEqual(runtime_config.provider, "supos_llm_gateway")
        self.assertEqual(runtime_config.model_id, "model-b")
        self.assertEqual(runtime_config.base_url, "http://supos.example/os/llm-gateway/v1")
        self.assertEqual(
            runtime_config.cache_key,
            "supos_llm_gateway|model-b|http://supos.example/os/llm-gateway/v1|0.2",
        )

    def test_agent_cache_reuses_same_model_config(self) -> None:
        import agent
        from utils.llm_model_factory import LlmRuntimeConfig

        agent.clear_data_insight_agent_cache()
        runtime_config = LlmRuntimeConfig(
            provider="supos_llm_gateway",
            model_id="model-b",
            base_url="http://supos.example/os/llm-gateway/v1",
            api_key="app-key",
            temperature=0.2,
            adapter="openai",
        )

        with patch("agent.create_data_insight_agent", side_effect=lambda _config: object()) as factory:
            first = agent.get_data_insight_agent(runtime_config)
            second = agent.get_data_insight_agent(runtime_config)

        self.assertIs(first, second)
        self.assertEqual(factory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
