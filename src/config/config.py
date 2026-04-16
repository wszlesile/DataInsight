import os
from dotenv import load_dotenv
from typing import Any

# 加载 .env 文件
load_dotenv()


def _get_env(name: str, default: str = '') -> str:
    return os.environ.get(name, default)


class Config:
    """应用配置类"""
    PROFILE = os.environ.get('PROFILE', 'local')
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key')
    DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'

    # 数据库配置统一使用平台规范变量名：
    # SUPOS_DATA_INSIGHT_*。
    # 本地开发和 Docker 联调也复用这套命名，避免出现两套口径。
    DB_RESOURCE_NAME = 'DATA_INSIGHT'
    DB_TYPE = _get_env('SUPOS_DATA_INSIGHT_DBTYPE', 'sqlite').lower()
    DB_HOST = _get_env('SUPOS_DATA_INSIGHT_HOST', 'localhost')
    DB_NAME = _get_env('SUPOS_DATA_INSIGHT_DBNAME', 'data_insight')
    DB_USER = _get_env('SUPOS_DATA_INSIGHT_USERNAME', 'root')
    DB_PASSWORD = _get_env('SUPOS_DATA_INSIGHT_PASSWORD', '')
    DB_URL = _get_env('SUPOS_DATA_INSIGHT_URL', '')
    DB_PORT = int(
        _get_env(
            'SUPOS_DATA_INSIGHT_PORT',
            '5432' if DB_TYPE in ('postgres', 'postgresql') else '3306',
        )
    )

    # SQLite 配置
    SQLITE_PATH = _get_env('SUPOS_DATA_INSIGHT_SQLITE_PATH', 'data_insight.db')
    SQLITE_BUSY_TIMEOUT_SECONDS = int(os.environ.get('SUPOS_DATA_INSIGHT_SQLITE_BUSY_TIMEOUT_SECONDS', 30))

    # 图表文件临时保存目录
    TEMP_DIR = os.environ.get('TEMP_DIR', 'D:/PycharmProjects/DataInsight/temp')
    # 外部上传文件保存目录
    UPLOAD_DIR = os.environ.get('UPLOAD_DIR', 'D:/PycharmProjects/DataInsight/uploads')

    # 日志配置
    LOG_DIR = os.environ.get('LOG_DIR', 'logs')
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    LOG_ENABLE_FILE = os.environ.get('LOG_ENABLE_FILE', 'true').lower() == 'true'
    LOG_RETENTION_DAYS = int(os.environ.get('LOG_RETENTION_DAYS', 15))
    # 分页配置
    DEFAULT_PAGE_SIZE = 10
    MAX_PAGE_SIZE = 100

    # 应用配置
    APP_NAME = "DataInsight App"
    VERSION = "1.0.0"

    # 用户系统配置
    USER_AUTH_ENDPOINT = '/os/inter-api/auth/v1/current-user/sessionInfo'
    USER_CONTEXT_CACHE_TTL_SECONDS = int(os.environ.get('USER_CONTEXT_CACHE_TTL_SECONDS', 600))

    # SUPOS / Kernel 配置
    SUPOS_WEB = os.environ.get('SUPOS_WEB', 'http://localhost:8080')
    SUPOS_REQUEST_TIMEOUT = int(os.environ.get('SUPOS_REQUEST_TIMEOUT', 15))
    SUPOS_LOG_COLLECT_TRACK_ENDPOINT = os.environ.get(
        'SUPOS_LOG_COLLECT_TRACK_ENDPOINT',
        '/os/inter-api/log-collect-system/track',
    )
    PYTHON_EXEC_TIMEOUT_SECONDS = int(os.environ.get('PYTHON_EXEC_TIMEOUT_SECONDS', 90))
    UNS_MAX_EXPANDED_FILES = int(os.environ.get('UNS_MAX_EXPANDED_FILES', 200))
    UNS_MAX_EXPAND_DEPTH = int(os.environ.get('UNS_MAX_EXPAND_DEPTH', 5))
    UNS_TREE_PAGE_SIZE = int(os.environ.get('UNS_TREE_PAGE_SIZE', 100))
    UNS_DETAIL_WORKERS = int(os.environ.get('UNS_DETAIL_WORKERS', 5))
    UNS_IMPORT_MAX_CONCURRENT = int(os.environ.get('UNS_IMPORT_MAX_CONCURRENT', 2))

    # LLM配置
    LLM_MODEL_ACTIVE=os.environ.get('LLM_MODEL_ACTIVE', 'MiniMax-M2.5')

    MINIMAX_M2_5_MODEL = os.environ.get('MINIMAX_M2_5_MODEL', 'MiniMax-M2.5')
    MINIMAX_M2_5_API_KEY = os.environ.get('MINIMAX_M2_5_API_KEY', '')
    MINIMAX_M2_5_BASE_URL = os.environ.get('MINIMAX_M2_5_BASE_URL', 'https://api.minimax.chat/v1')

    QWEN3_80B_MODEL =  os.environ.get('QWEN3_80B_MODEL', 'Qwen3-80B')
    QWEN3_80B_API_KEY = os.environ.get('QWEN3_80B_API_KEY', 'EMPTY')
    QWEN3_80B_BASE_URL = os.environ.get('QWEN3_80B_BASE_URL', 'http://192.168.8.206:5103/v1')

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return getattr(cls, key, default)
