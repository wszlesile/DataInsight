import os
from config.factory import create_app
from config.config import Config


def main():
    app = create_app(Config)
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    debug = Config.DEBUG

    print(f"启动服务器: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    main()
