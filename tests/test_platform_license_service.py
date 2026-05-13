import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.platform_license_service import LicenseExpiredError, PlatformLicenseService


def _license_payload(*, deadline: int, expired_status: int = 0) -> dict:
    return {
        "code": 0,
        "message": "success",
        "data": {
            "products": [
                {
                    "id": 7000,
                    "features": [
                        {
                            "id": 7121,
                            "deadline": deadline,
                            "expiredStatus": expired_status,
                        }
                    ],
                }
            ]
        },
    }


class FakeLicenseApi:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls = 0

    def fetch_license_detail(self, authorization: str, feature_id: int) -> dict:
        self.calls += 1
        return self.payloads.pop(0)


class PlatformLicenseServiceTestCase(unittest.TestCase):
    def test_expired_cache_refreshes_after_cache_interval(self) -> None:
        now = 1_000
        api = FakeLicenseApi([
            _license_payload(deadline=900, expired_status=2),
            _license_payload(deadline=260_000, expired_status=0),
        ])
        service = PlatformLicenseService(
            license_api=api,
            now_ms=lambda: now,
            refresh_interval_ms=60_000,
        )

        with self.assertRaises(LicenseExpiredError):
            service.ensure_agent_authorized("Bearer token")

        now = 62_000
        service.ensure_agent_authorized("Bearer token")

        self.assertEqual(api.calls, 2)

    def test_fresh_expired_cache_still_blocks_without_refresh(self) -> None:
        now = 1_000
        api = FakeLicenseApi([
            _license_payload(deadline=900, expired_status=2),
        ])
        service = PlatformLicenseService(
            license_api=api,
            now_ms=lambda: now,
            refresh_interval_ms=60_000,
        )

        with self.assertRaises(LicenseExpiredError):
            service.ensure_agent_authorized("Bearer token")

        now = 2_000
        with self.assertRaises(LicenseExpiredError):
            service.ensure_agent_authorized("Bearer token")

        self.assertEqual(api.calls, 1)

    def test_near_expired_status_is_authorized_when_deadline_is_valid(self) -> None:
        now = 1_000
        api = FakeLicenseApi([
            _license_payload(deadline=260_000, expired_status=1),
        ])
        service = PlatformLicenseService(
            license_api=api,
            now_ms=lambda: now,
            refresh_interval_ms=60_000,
        )

        service.ensure_agent_authorized("Bearer token")

        self.assertEqual(api.calls, 1)


if __name__ == "__main__":
    unittest.main()
