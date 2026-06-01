import os
from contextlib import contextmanager
from typing import Callable, TypeVar

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import text

from config.config import Config

load_dotenv()

db_type = Config.DB_TYPE.lower()
if db_type == 'postgres':
    db_type = 'postgresql'

DB_POOL_RECYCLE_SECONDS = int(os.environ.get('SUPOS_DATA_INSIGHT_POOL_RECYCLE_SECONDS', 1800))

if db_type == 'sqlite':
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        Config.SQLITE_PATH,
    )
    engine = create_engine(f"sqlite:///{db_path}", echo=Config.DEBUG)
elif db_type == 'mysql':
    engine = create_engine(
        URL.create(
            drivername='mysql+pymysql',
            username=Config.DB_USER,
            password=Config.DB_PASSWORD,
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            database=Config.DB_NAME,
        ),
        pool_pre_ping=True,
        pool_recycle=DB_POOL_RECYCLE_SECONDS,
        echo=Config.DEBUG,
    )
elif db_type == 'postgresql':
    engine = create_engine(
        URL.create(
            drivername='postgresql+psycopg2',
            username=Config.DB_USER,
            password=Config.DB_PASSWORD,
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            database=Config.DB_NAME,
        ),
        pool_pre_ping=True,
        pool_recycle=DB_POOL_RECYCLE_SECONDS,
        echo=Config.DEBUG,
    )
else:
    raise ValueError(f"Unsupported database type: {db_type}")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
POSTGRES_SCHEMA_LOCK_KEY = 827364219
T = TypeVar('T')


def _is_disconnect_error(exc: Exception) -> bool:
    if isinstance(exc, DBAPIError) and getattr(exc, 'connection_invalidated', False):
        return True
    return False


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope(*, expire_on_commit: bool | None = None):
    """Create a short-lived ORM session for one business operation."""
    session_kwargs = {}
    if expire_on_commit is not None:
        session_kwargs['expire_on_commit'] = expire_on_commit
    session = SessionLocal(**session_kwargs)
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_with_session(
    operation: Callable[[object], T],
    *,
    retry_on_disconnect: bool = True,
    expire_on_commit: bool | None = False,
) -> T:
    """
    Execute a unit of work with a fresh ORM session.

    If the checked-out DB connection was invalidated, retry once with a new
    session so callers do not need scattered stale-session recovery logic.
    """
    attempts = 2 if retry_on_disconnect else 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with session_scope(expire_on_commit=expire_on_commit) as session:
                return operation(session)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts - 1 or not _is_disconnect_error(exc):
                raise
            engine.dispose()
    raise last_error if last_error is not None else RuntimeError('session operation failed')


def init_db():
    """
    根据当前 ORM 模型创建缺失表。

    当前项目不再在启动阶段承载历史 SQLite 迁移或补丁式修表逻辑，
    数据库结构升级应通过显式的迁移方案处理，而不是在这里隐式改表。
    """
    if db_type == 'postgresql':
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": POSTGRES_SCHEMA_LOCK_KEY})
            Base.metadata.create_all(bind=connection)
        return

    Base.metadata.create_all(bind=engine)
