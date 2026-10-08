import os
import threading
from config.factory import create_app
from config.config import Config


def _start_task_runner() -> threading.Thread:
    from insight_task.runner import InsightTaskRunner

    thread = threading.Thread(
        target=InsightTaskRunner().run_forever,
        name='insight-task-runner',
        daemon=True,
    )
    thread.start()
    return thread


def _start_evidence_runner() -> threading.Thread:
    from service.evidence_task_runner import EvidenceTaskRunner

    thread = threading.Thread(
        target=EvidenceTaskRunner().run_forever,
        name='insight-evidence-runner',
        daemon=True,
    )
    thread.start()
    return thread


def main():
    role = Config.INSIGHT_APP_ROLE
    if role == 'task-runner':
        from insight_task.runner import InsightTaskRunner

        _start_evidence_runner()
        InsightTaskRunner().run_forever()
        return
    if role not in {'all', 'api'}:
        raise SystemExit(f'Unsupported INSIGHT_APP_ROLE: {role}')

    app = create_app(Config)
    if role == 'all':
        _start_task_runner()
        _start_evidence_runner()
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    debug = Config.DEBUG

    print(f"启动服务器: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == '__main__':
    main()
