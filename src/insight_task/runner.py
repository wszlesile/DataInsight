import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from api import supos_kernel_api
from config import Config
from config.database import SessionLocal
from insight_task.dispatcher import InsightTaskDispatcher, InsightTaskDispatchError
from insight_task.service import InsightTaskService
from utils import logger


class InsightTaskRunner:
    def __init__(self):
        self.worker_id = f'{socket.gethostname()}:{os.getpid()}'
        self.dispatcher = InsightTaskDispatcher()

    def run_forever(self) -> None:
        logger.info('洞察任务 Runner 已启动: worker_id=%s', self.worker_id)
        poll_seconds = max(1.0, Config.INSIGHT_TASK_SCHEDULER_POLL_SECONDS)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.exception('洞察任务调度循环异常: %s', exc)
            time.sleep(poll_seconds)

    def run_once(self) -> int:
        session = SessionLocal()
        try:
            service = InsightTaskService(session)
            service.recover_expired_runs()
            service.enqueue_due_runs(limit=max(1, Config.INSIGHT_TASK_MAX_CONCURRENCY * 2))
            queued = service.list_queued_runs(limit=max(1, Config.INSIGHT_TASK_MAX_CONCURRENCY))
        finally:
            session.close()

        run_ids = [str(item.get('run_id') or '') for item in queued if item.get('run_id')]
        if not run_ids:
            return 0
        completed = 0
        with ThreadPoolExecutor(max_workers=max(1, Config.INSIGHT_TASK_MAX_CONCURRENCY)) as executor:
            futures = {executor.submit(self._dispatch_one, run_id): run_id for run_id in run_ids}
            for future in as_completed(futures):
                if future.result():
                    completed += 1
        return completed

    def _dispatch_one(self, run_id: str) -> bool:
        try:
            if Config.PROFILE == 'local':
                self._execute_local(run_id)
            else:
                self.dispatcher.dispatch(run_id)
            return True
        except Exception as exc:
            logger.exception('洞察任务派发失败: run_id=%s error=%s', run_id, exc)
            failed_session = SessionLocal()
            try:
                InsightTaskService(failed_session).record_dispatch_failure(
                    run_id=run_id,
                    error_message=str(exc),
                    retryable=isinstance(exc, InsightTaskDispatchError) and exc.retryable,
                )
            finally:
                failed_session.close()
            return False

    def _execute_local(self, run_id: str) -> None:
        platform_authorization = str(Config.LOCAL_ORIGIN_TOKEN or '').strip()
        fedquery_authorization = str(Config.LOCAL_GATEWAY_JWT or '').strip()
        if not platform_authorization or not fedquery_authorization:
            raise InsightTaskDispatchError(
                '本地任务 Runner 缺少 DATAINSIGHT_LOCAL_ORIGIN_TOKEN 或 DATAINSIGHT_LOCAL_GATEWAY_JWT',
                retryable=False,
            )

        database_conn_info = supos_kernel_api.get_database_conn_info(platform_authorization)
        runtime_user_context = {
            'platform_authorization': platform_authorization,
            'fedquery_authorization': fedquery_authorization,
            'database_conn_info': {
                'host': getattr(database_conn_info, 'host', '') or '',
                'port': getattr(database_conn_info, 'port', '') or '',
                'user': getattr(database_conn_info, 'user', '') or '',
                'password': getattr(database_conn_info, 'password', '') or '',
                'lake_rds_database_name': getattr(database_conn_info, 'lake_rds_database_name', '') or '',
                'ts_query_source': getattr(database_conn_info, 'ts_query_source', '') or '',
            },
        }
        session = SessionLocal()
        try:
            result = InsightTaskService(session).execute_run(
                run_id=run_id,
                execution_principal='local-task-runner',
                runtime_user_context=runtime_user_context,
                worker_id=f'{self.worker_id}:local',
                include_result=False,
            )
            if result is None:
                raise InsightTaskDispatchError('洞察任务运行记录不存在', retryable=False)
        finally:
            session.close()


def main() -> None:
    InsightTaskRunner().run_forever()


if __name__ == '__main__':
    main()
