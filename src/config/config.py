import os
from dotenv import load_dotenv
from typing import Any

# 加载 .env 文件
load_dotenv()


class Config:
    """应用配置类"""

    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key')
    DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'

    # 数据库类型: mysql, postgresql, sqlite
    DB_TYPE = os.environ.get('DB_TYPE', 'sqlite')

    # MySQL配置
    DB_HOST = os.environ.get('DB_HOST', 'localhost')
    DB_PORT = int(os.environ.get('DB_PORT', 3306))
    DB_NAME = os.environ.get('DB_NAME', 'data_insight')
    DB_USER = os.environ.get('DB_USER', 'root')
    DB_PASSWORD = os.environ.get('DB_PASSWORD', '')

    # Postgresql配置
    PG_HOST = os.environ.get('PG_HOST', 'localhost')
    PG_PORT = int(os.environ.get('PG_PORT', 5432))
    PG_NAME = os.environ.get('PG_NAME', 'data_insight')
    PG_USER = os.environ.get('PG_USER', 'postgres')
    PG_PASSWORD = os.environ.get('PG_PASSWORD', '')

    # SQLite配置
    SQLITE_PATH = os.environ.get('SQLITE_PATH', 'data_insight.db')

    # 图表文件临时保存目录
    TEMP_DIR = os.environ.get('TEMP_DIR', 'D:/PycharmProjects/DataInsight/temp')
    # 外部上传文件保存目录
    UPLOAD_DIR = os.environ.get('UPLOAD_DIR', 'D:/PycharmProjects/DataInsight/uploads')

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
    SUPOS_KERNEL_HOST = os.environ.get('SUPOS_KERNEL_HOST', 'platform-base-kernel')
    SUPOS_KERNEL_PORT = os.environ.get('SUPOS_KERNEL_PORT', '6443')
    SUPOS_KERNEL_TOKEN = os.environ.get('SUPOS_KERNEL_TOKEN', '')
    SUPOS_REQUEST_TIMEOUT = int(os.environ.get('SUPOS_REQUEST_TIMEOUT', 15))

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
