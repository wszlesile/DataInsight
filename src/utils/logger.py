import logging
import sys


class Logger:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._init_logger()
        return cls._instance

    def _init_logger(self):
        self.logger = logging.getLogger('data_insight')
        self.logger.setLevel(logging.DEBUG)

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter('[%(asctime)s] %(levelname)s in %(name)s: %(message)s')
        handler.setFormatter(formatter)

        self.logger.addHandler(handler)

    @staticmethod
    def _safe_message(message: str) -> str:
        text = str(message)
        encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        return text.encode(encoding, errors='replace').decode(encoding, errors='replace')

    def _format_message(self, message: str, *args) -> str:
        if args:
            try:
                message = message % args
            except Exception:
                message = f"{message} {' '.join(str(arg) for arg in args)}"
        return self._safe_message(message)

    def debug(self, message: str, *args, **kwargs):
        self.logger.debug(self._format_message(message, *args), **kwargs)

    def info(self, message: str, *args, **kwargs):
        self.logger.info(self._format_message(message, *args), **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self.logger.warning(self._format_message(message, *args), **kwargs)

    def warn(self, message: str, *args, **kwargs):
        self.warning(message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        self.logger.error(self._format_message(message, *args), **kwargs)

    def critical(self, message: str, *args, **kwargs):
        self.logger.critical(self._format_message(message, *args), **kwargs)


logger = Logger()
