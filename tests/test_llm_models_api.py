import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from api.SuposkernelApi import SuposKernelApi  # noqa: E402
from controller.llm_controller import LlmController  # noqa: E402


class LlmModelsApiTestCase(unittest.TestCase):
    def test_fetch_llm_gateway_models_uses_platform_endpoint_and_authorization(self) -> None:
        api = SuposKernelApi()
        api.supos_web = "http://supos.example"
        api.timeout = 7
        payload = {
            "data": [
                {
                    "id": "360GPT_S2_V9",
                    "object": "model",
                    "created": 1677649963,
                    "owned_by": "360",
                }
            ],
            "object": "list",
        }
        response = Mock()
        response.json.return_value = payload

        with patch("api.SuposkernelApi.requests.get", return_value=response) as request_get:
            result = api.fetch_llm_gateway_models("Bearer test-token")

        request_get.assert_called_once_with(
            "http://supos.example/os/llm-gateway/v1/models",
            headers={"Authorization": "Bearer test-token"},
            timeout=7,
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(result, payload)

    def test_llm_controller_prefers_gateway_app_key_for_model_list(self) -> None:
        with patch("controller.llm_controller.Config") as config:
            config.SUPOS_LLM_GATEWAY_API_KEY = "app-key"
            controller = LlmController(Mock())

            authorization = controller._get_llm_gateway_authorization(
                Mock(token="Bearer user-token")
            )

        self.assertEqual(authorization, "Bearer app-key")


if __name__ == "__main__":
    unittest.main()
