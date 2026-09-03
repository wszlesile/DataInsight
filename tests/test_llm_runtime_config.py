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
            config.SUPOS_LLM_GATEWAY_API_KEY = "app-key"
            config.BASE_URL = "http://llm-gateway.example/v1"
            config.TEMPERATURE = 0.2

            runtime_config = create_runtime_config(selected_model_id="model-b")

        self.assertEqual(runtime_config.provider, "supos_llm_gateway")
        self.assertEqual(runtime_config.model_id, "model-b")
        self.assertEqual(runtime_config.base_url, "http://llm-gateway.example/v1")
        self.assertEqual(
            runtime_config.cache_key,
            "supos_llm_gateway|model-b|http://llm-gateway.example/v1|0.2",
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

        with patch(
            "agent.create_data_insight_agent",
            side_effect=lambda _config, **_kwargs: object(),
        ) as factory:
            first = agent.get_data_insight_agent(runtime_config)
            second = agent.get_data_insight_agent(runtime_config)

        self.assertIs(first, second)
        self.assertEqual(factory.call_count, 1)

    def test_analysis_agent_cache_separates_clarification_modes(self) -> None:
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

        with patch(
            "agent.create_data_insight_agent",
            side_effect=lambda _config, **_kwargs: object(),
        ) as factory:
            interactive = agent.get_data_insight_agent(
                runtime_config,
                clarification_mode="same_turn",
            )
            workflow = agent.get_data_insight_agent(
                runtime_config,
                clarification_mode="disabled",
            )

        self.assertIsNot(interactive, workflow)
        self.assertEqual(factory.call_count, 2)

    def test_analysis_agent_cache_reuses_the_same_capabilities_across_rounds(self) -> None:
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

        with patch(
            "agent.create_data_insight_agent",
            side_effect=lambda _config, **_kwargs: object(),
        ) as factory:
            before_probe = agent.get_data_insight_agent(
                runtime_config,
            )
            after_probe = agent.get_data_insight_agent(
                runtime_config,
            )

        self.assertIs(before_probe, after_probe)
        self.assertEqual(factory.call_count, 1)

    def test_general_agent_cache_is_separate_from_analysis_agent(self) -> None:
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

        with patch("agent.create_general_conversation_agent", side_effect=lambda _config: object()) as factory:
            first = agent.get_general_conversation_agent(runtime_config)
            second = agent.get_general_conversation_agent(runtime_config)

        self.assertIs(first, second)
        self.assertEqual(factory.call_count, 1)

    def test_general_agent_has_no_analysis_tools(self) -> None:
        import agent

        with (
            patch("agent.create_data_insight_model", return_value=object()),
            patch("agent.create_agent", return_value=object()) as factory,
        ):
            agent.create_general_conversation_agent()

        self.assertEqual(factory.call_args.kwargs["tools"], [])


if __name__ == "__main__":
    unittest.main()
