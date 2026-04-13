import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker

from config.config import Config

load_dotenv()

db_type = Config.DB_TYPE.lower()
if db_type == 'postgres':
    db_type = 'postgresql'

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
        echo=Config.DEBUG,
    )
else:
    raise ValueError(f"Unsupported database type: {db_type}")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db():
    """
    根据当前 ORM 模型创建缺失表。

    当前项目不再在启动阶段承载历史 SQLite 迁移或补丁式修表逻辑，
    数据库结构升级应通过显式的迁移方案处理，而不是在这里隐式改表。
    """
    Base.metadata.create_all(bind=engine)
