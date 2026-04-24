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
    LOCAL_FILE_LOW_MEMORY_THRESHOLD_MB = int(os.environ.get('LOCAL_FILE_LOW_MEMORY_THRESHOLD_MB', 100))
    LOCAL_FILE_LOW_MEMORY_THRESHOLD_BYTES = LOCAL_FILE_LOW_MEMORY_THRESHOLD_MB * 1024 * 1024
    UNS_MAX_EXPANDED_FILES = int(os.environ.get('UNS_MAX_EXPANDED_FILES', 200))
    UNS_MAX_EXPAND_DEPTH = int(os.environ.get('UNS_MAX_EXPAND_DEPTH', 5))
    UNS_TREE_PAGE_SIZE = int(os.environ.get('UNS_TREE_PAGE_SIZE', 100))
    UNS_DETAIL_WORKERS = int(os.environ.get('UNS_DETAIL_WORKERS', 5))
    UNS_IMPORT_MAX_CONCURRENT = int(os.environ.get('UNS_IMPORT_MAX_CONCURRENT', 2))

    # LLM配置
    LLM_MODEL_ACTIVE = os.environ.get('LLM_MODEL_ACTIVE', 'minimax')
    MODEL = os.environ.get('MODEL', 'MiniMax-M2.5')
    API_KEY = os.environ.get('API_KEY', '')
    BASE_URL = os.environ.get('BASE_URL', 'https://api.minimax.chat/v1')
    TEMPERATURE = float(os.environ.get('TEMPERATURE', 0.7))

    CONTEXT_COMPRESSION_ENABLED = os.environ.get('CONTEXT_COMPRESSION_ENABLED', 'true').lower() == 'true'
    CONTEXT_COMPRESSION_USE_LLM = os.environ.get('CONTEXT_COMPRESSION_USE_LLM', 'true').lower() == 'true'
    CONTEXT_COMPRESSION_TRIGGER_TURNS = int(os.environ.get('CONTEXT_COMPRESSION_TRIGGER_TURNS', 6))
    CONTEXT_COMPRESSION_KEEP_RECENT_TURNS = int(os.environ.get('CONTEXT_COMPRESSION_KEEP_RECENT_TURNS', 3))
    CONTEXT_COMPRESSION_HISTORY_MESSAGE_LIMIT = int(os.environ.get('CONTEXT_COMPRESSION_HISTORY_MESSAGE_LIMIT', 10))
    CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS = int(os.environ.get('CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS', 4000))
    CONTEXT_COMPRESSION_PROMPT_MAX_TOKENS = int(os.environ.get('CONTEXT_COMPRESSION_PROMPT_MAX_TOKENS', 24000))
    CONTEXT_COMPRESSION_HISTORY_TOKEN_RATIO = float(os.environ.get('CONTEXT_COMPRESSION_HISTORY_TOKEN_RATIO', 0.55))
    CONTEXT_COMPRESSION_MIN_HISTORY_TOKENS = int(os.environ.get('CONTEXT_COMPRESSION_MIN_HISTORY_TOKENS', 2500))
    CONTEXT_COMPRESSION_MIN_MEMORY_TOKENS = int(os.environ.get('CONTEXT_COMPRESSION_MIN_MEMORY_TOKENS', 1800))
    CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS = int(
        os.environ.get('CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS', 800)
    )
    CONTEXT_COMPRESSION_EXECUTION_CODE_CHARS = int(
        os.environ.get('CONTEXT_COMPRESSION_EXECUTION_CODE_CHARS', 3000)
    )
    CONTEXT_COMPRESSION_ARTIFACT_SUMMARY_CHARS = int(
        os.environ.get('CONTEXT_COMPRESSION_ARTIFACT_SUMMARY_CHARS', 500)
    )
    DATASOURCE_CONTEXT_MAX_COUNT = int(os.environ.get('DATASOURCE_CONTEXT_MAX_COUNT', 10))

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return getattr(cls, key, default)
