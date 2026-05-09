import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable

from api import supos_kernel_api
from config.config import Config


AGENT_FEATURE_ID = 7121


class PlatformLicenseError(Exception):
    """Base error for platform license checks."""


class LicenseUnauthorizedError(PlatformLicenseError):
    """Raised when the platform has no DataInsight Agent authorization."""


class LicenseExpiredError(PlatformLicenseError):
    """Raised when the DataInsight Agent authorization is expired."""


class LicenseUnavailableError(PlatformLicenseError):
    """Raised when the platform license service cannot be reached or parsed."""


@dataclass
class LicenseCache:
    deadline: int
    expired_status: int
    checked_at: int


class PlatformLicenseService:
    """Lightweight platform license cache and validator."""

    def __init__(
        self,
        license_api=None,
        now_ms: Callable[[], int] | None = None,
        refresh_interval_ms: int | None = None,
    ):
        self.license_api = license_api or supos_kernel_api
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.refresh_interval_ms = (
            int(refresh_interval_ms)
            if refresh_interval_ms is not None
            else int(getattr(Config, "PLATFORM_LICENSE_CACHE_REFRESH_SECONDS", 300) or 300) * 1000
        )
        self._cache: LicenseCache | None = None
        self._lock = Lock()

    def ensure_agent_authorized(self, authorization: str) -> None:
        if not authorization:
            raise LicenseUnauthorizedError("缺少平台授权校验凭证")

        now = self.now_ms()
        if self._is_cache_usable(now):
            return

        with self._lock:
            now = self.now_ms()
            if self._is_cache_usable(now):
                return
            self._refresh_cache(authorization, now)

    def _is_cache_usable(self, now: int) -> bool:
        cache = self._cache
        if cache is None:
            return False
        if cache.expired_status != 0 or cache.deadline <= now:
            raise LicenseExpiredError("数据洞察智能体授权已到期")
        return now - cache.checked_at < self.refresh_interval_ms

    def _refresh_cache(self, authorization: str, now: int) -> None:
        try:
            payload = self.license_api.fetch_license_detail(
                authorization=authorization,
                feature_id=AGENT_FEATURE_ID,
            )
        except PlatformLicenseError:
            raise
        except Exception as exc:
            raise LicenseUnavailableError("获取平台授权失败，请稍后重试") from exc

        feature = self._extract_agent_feature(payload)
        deadline = self._extract_deadline(feature)
        expired_status = int(feature.get("expiredStatus") or 0)
        if expired_status != 0 or deadline <= now:
            self._cache = LicenseCache(
                deadline=deadline,
                expired_status=expired_status,
                checked_at=now,
            )
            raise LicenseExpiredError("数据洞察智能体授权已到期")

        self._cache = LicenseCache(
            deadline=deadline,
            expired_status=expired_status,
            checked_at=now,
        )

    def _extract_agent_feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise LicenseUnavailableError("平台授权响应格式错误")
        if payload.get("code") != 0:
            raise LicenseUnavailableError(str(payload.get("message") or "获取平台授权失败"))

        data = payload.get("data") or {}
        products = data.get("products") if isinstance(data, dict) else []
        if not isinstance(products, list):
            raise LicenseUnavailableError("平台授权响应格式错误")

        for product in products:
            if not isinstance(product, dict):
                continue
            features = product.get("features") or []
            if not isinstance(features, list):
                continue
            for feature in features:
                if isinstance(feature, dict) and int(feature.get("id") or 0) == AGENT_FEATURE_ID:
                    return feature
        raise LicenseUnauthorizedError("当前平台未授权数据洞察智能体")

    def _extract_deadline(self, feature: dict[str, Any]) -> int:
        try:
            deadline = int(feature.get("deadline") or 0)
        except (TypeError, ValueError):
            deadline = 0
        if deadline <= 0:
            raise LicenseUnavailableError("平台授权响应缺少有效截止时间")
        return deadline


platform_license_service = PlatformLicenseService()
