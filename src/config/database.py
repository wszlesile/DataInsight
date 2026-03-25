import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config.config import Config

# 加载 .env 文件
load_dotenv()

# 根据数据库类型创建引擎
db_type = Config.DB_TYPE.lower()

if db_type == 'sqlite':
    # SQLite
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), Config.SQLITE_PATH)
    engine = create_engine(f"sqlite:///{db_path}", echo=Config.DEBUG)
elif db_type == 'mysql':
    # MySQL
    engine = create_engine(
        f"mysql://{Config.DB_USER}:{Config.DB_PASSWORD}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}",
        pool_pre_ping=True,
        echo=Config.DEBUG
    )

elif db_type == 'postgresql':
    # postgresql
    engine = create_engine(
        f"postgresql://{Config.PG_USER}:{Config.PG_PASSWORD}@{Config.PG_HOST}:{Config.PG_PORT}/{Config.PG_NAME}",
        pool_pre_ping=True,
        echo=Config.DEBUG
    )

else:
    raise ValueError(f"不支持的数据库类型: {db_type}")

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 创建声明式基类
Base = declarative_base()


def get_session():
    """获取数据库会话"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db():
    """初始化数据库表"""
    Base.metadata.create_all(bind=engine)
