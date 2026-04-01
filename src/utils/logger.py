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

    def debug(self, message: str):
        self.logger.debug(self._safe_message(message))

    def info(self, message: str):
        self.logger.info(self._safe_message(message))

    def warning(self, message: str):
        self.logger.warning(self._safe_message(message))

    def error(self, message: str):
        self.logger.error(self._safe_message(message))

    def critical(self, message: str):
        self.logger.critical(self._safe_message(message))


logger = Logger()
