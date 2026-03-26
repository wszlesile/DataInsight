from typing import Any

from flask import Flask
from sqlalchemy.orm import Session

from config.config import Config
from config.database import SessionLocal, init_db
from controller.user_controller import create_user_controller
from dao.user_dao import UserDAO
from service.user_service import UserService
from utils import logger


# bean工厂
class BeanFactory:
    _instance = None

    def __init__(self):
        self.instanceDict:dict[str, Any]= {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance
beanFactory:BeanFactory = BeanFactory()

def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    init_db()
    _init_components()
    _register_routes(app)

    logger.info(f"应用 {config_class.APP_NAME} 启动成功")
    return app

def _init_components():
    session: Session = SessionLocal()
    try:
       user_dao = UserDAO(session,beanFactory)
       UserService(user_dao,beanFactory)
    except Exception as e:
        session.close()
        logger.error(f"组件初始化失败: {e}")
        raise


def _register_routes(app: Flask):
    user_blueprint = create_user_controller(beanFactory.user_service)
    app.register_blueprint(user_blueprint)

    @app.route('/health')
    def health():
        return {"status": "ok", "app": app.config.get('APP_NAME')}

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        if hasattr(app, 'session'):
            app.session.close()
