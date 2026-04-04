import os
from typing import Any

from flask import Flask, abort, send_from_directory

from config.config import Config
from config.database import init_db
from controller.agent_controller import create_agent_controller
from controller.insight_namespace_controller import create_insight_namespace_controller
from controller.insight_ns_conversation_controller import create_insight_ns_conversation_controller
from controller.insight_user_collect_controller import create_insight_user_collect_controller
from middleware.auth_middleware import init_auth_middleware
from utils import logger


class BeanFactory:
    _instance = None

    def __init__(self):
        self.instanceDict: dict[str, Any] = {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance


beanFactory: BeanFactory = BeanFactory()


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
    init_auth_middleware(app)

    app.register_blueprint(create_agent_controller())
    app.register_blueprint(create_insight_namespace_controller())
    app.register_blueprint(create_insight_ns_conversation_controller())
    app.register_blueprint(create_insight_user_collect_controller())

    @app.route('/health')
    def health():
        return {"status": "ok", "app": app.config.get('APP_NAME')}

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        if hasattr(app, 'session'):
            app.session.close()

    @app.route('/files/<path:filename>')
    def serve_file(filename):
        file_path = filename.replace('/', os.sep).replace('\\', os.sep)
        if not os.path.isabs(file_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, file_path)
        if os.path.isfile(file_path):
            return send_from_directory(os.path.dirname(file_path), os.path.basename(file_path))
        abort(404)
