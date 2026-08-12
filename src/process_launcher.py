import os
import signal
import subprocess
import sys
import time


def _gunicorn_command() -> list[str]:
    return ['gunicorn', '-c', 'docker/gunicorn.conf.py', 'wsgi:app']


def _runner_command() -> list[str]:
    return [sys.executable, '-m', 'insight_task.runner']


def _exec(command: list[str]) -> None:
    os.execvp(command[0], command)


def _supervise(commands: list[list[str]]) -> None:
    processes = [subprocess.Popen(command) for command in commands]

    def terminate_all(signum=None, frame=None):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, terminate_all)
    signal.signal(signal.SIGINT, terminate_all)
    try:
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    terminate_all()
                    for sibling in processes:
                        if sibling is not process:
                            try:
                                sibling.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                sibling.kill()
                    raise SystemExit(exit_code)
            time.sleep(0.5)
    finally:
        terminate_all()


def main() -> None:
    role = os.environ.get('INSIGHT_APP_ROLE', 'all').strip().lower()
    if role == 'api':
        _exec(_gunicorn_command())
    if role == 'task-runner':
        _exec(_runner_command())
    if role != 'all':
        raise SystemExit(f'Unsupported INSIGHT_APP_ROLE: {role}')
    _supervise([_gunicorn_command(), _runner_command()])


if __name__ == '__main__':
    main()
