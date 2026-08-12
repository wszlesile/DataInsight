import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import Config


class InsightTaskDispatchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class InsightTaskDispatcher:
    """Dispatch a queued run through Gateway so it receives a fresh service JWT."""

    def dispatch(self, run_id: str) -> dict:
        app_key = Config.SUPOS_DATAINSIGHT_SERVER_APPKEY
        if not app_key:
            raise RuntimeError('SUPOS_DATAINSIGHT-SERVER_APPKEY 未配置')
        path = Config.INSIGHT_TASK_INTERNAL_EXECUTE_PATH.format(run_id=run_id)
        url = f"{Config.SUPOS_GATEWAY.rstrip('/')}/{path.lstrip('/')}"
        authorization = app_key if app_key.startswith('Bearer ') else f'Bearer {app_key}'
        request = Request(
            url,
            data=b'{}',
            method='POST',
            headers={
                'Authorization': authorization,
                'Content-Type': 'application/json',
                'X-Insight-Task-Run-ID': run_id,
            },
        )
        try:
            with urlopen(request, timeout=max(1, Config.INSIGHT_TASK_DISPATCH_TIMEOUT_SECONDS)) as response:
                try:
                    payload = json.loads(response.read().decode('utf-8') or '{}')
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InsightTaskDispatchError('任务执行接口返回了无法解析的响应', retryable=True) from exc
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            raise InsightTaskDispatchError(
                f'任务执行接口返回 HTTP {exc.code}: {body[:500]}',
                retryable=exc.code in {408, 409, 425, 429} or exc.code >= 500,
            ) from exc
        except URLError as exc:
            raise InsightTaskDispatchError(f'任务执行接口不可用: {exc.reason}', retryable=True) from exc
        except TimeoutError as exc:
            raise InsightTaskDispatchError('任务执行接口请求超时', retryable=True) from exc
        if not payload.get('success'):
            raise InsightTaskDispatchError(
                str(payload.get('message') or '任务执行接口返回失败'),
                retryable=False,
            )
        return payload.get('data') or {}
