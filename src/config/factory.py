from typing import Any

from flask import Flask, send_from_directory, abort
from sqlalchemy.orm import Session
import os

from config.config import Config
from config.database import SessionLocal, init_db
from controller.agent_controller import create_agent_controller
from middleware.auth_middleware import init_auth_middleware
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
    pass


def _register_routes(app: Flask):
    # 初始化认证中间件
    init_auth_middleware(app)

    # 注册 Agent 控制器
    agent_blueprint = create_agent_controller()
    app.register_blueprint(agent_blueprint)

    @app.route('/health')
    def health():
        return {"status": "ok", "app": app.config.get('APP_NAME')}

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        if hasattr(app, 'session'):
            app.session.close()

    # 提供静态文件访问（图表HTML文件）
    @app.route('/files/<path:filename>')
    def serve_file(filename):
        # filename 可能是完整路径如 D:/PycharmProjects/DataInsight/today_alarms_distribution.html
        # 也可能是文件资源id
        file_path = filename.replace('/', os.sep).replace('\\', os.sep)
        if not os.path.isabs(file_path):
            # 如果是相对路径，则在项目根目录下查找
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, file_path)
        if os.path.isfile(file_path):
            return send_from_directory(os.path.dirname(file_path), os.path.basename(file_path))
        abort(404)
