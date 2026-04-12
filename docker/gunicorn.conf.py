import multiprocessing
import os


# Gunicorn 绑定地址，默认监听容器内 5000 端口。
bind = os.getenv('GUNICORN_BIND', '0.0.0.0:5000')

# worker 数量默认按 CPU 粗略折中计算，最低保留 2 个。
workers = int(os.getenv('WEB_CONCURRENCY', max(2, multiprocessing.cpu_count() // 2)))

# 线程数适合当前 Flask + I/O 型请求场景。
threads = int(os.getenv('GUNICORN_THREADS', '4'))

# 当前默认使用 gthread，避免引入额外协程兼容成本。
worker_class = os.getenv('GUNICORN_WORKER_CLASS', 'gthread')

# 分析请求可能较长，超时时间保持相对宽松。
timeout = int(os.getenv('GUNICORN_TIMEOUT', '600'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', '60'))
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', '30'))

# 日志继续走 stdout / stderr，方便容器平台采集。
accesslog = '-'
errorlog = '-'
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')
capture_output = True
