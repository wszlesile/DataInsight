from typing import Dict

from flask import Flask
from sqlalchemy.orm import Session

from config.config import Config
from config.database import SessionLocal, init_db
from controller.user_controller import create_user_controller
from dao import BaseDAO
from dao.user_dao import UserDAO
from service.user_service import UserService
from utils.logger import logger


#
class DaoFactory:
    __instanceDict:dict[str, BaseDAO]
    def __init__(self):
        self.__instanceDict: Dict[str, BaseDAO] = {}
    def add(self,key:str,value:BaseDAO):
        self.__instanceDict[key] = value
    def get(self,key:str):
        return self.__instanceDict[key]


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    init_db()
    _init_components(app)
    _register_routes(app)

    logger.info(f"应用 {config_class.APP_NAME} 启动成功")
    return app

def _init_components(app: Flask):
    session: Session = SessionLocal()

    try:
        user_dao = UserDAO(session)
        app.user_dao = user_dao

        user_service = UserService(user_dao)
        app.user_service = user_service

        app.session = session
    except Exception as e:
        session.close()
        logger.error(f"组件初始化失败: {e}")
        raise


def _register_routes(app: Flask):
    user_blueprint = create_user_controller(app.user_service)
    app.register_blueprint(user_blueprint)

    @app.route('/health')
    def health():
        return {"status": "ok", "app": app.config.get('APP_NAME')}

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        if hasattr(app, 'session'):
            app.session.close()
