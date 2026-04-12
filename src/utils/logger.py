import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from config.config import Config


_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar('data_insight_log_context', default={})
_LOG_FIELDS = (
    'request_id',
    'username',
    'namespace_id',
    'conversation_id',
    'turn_id',
    'execution_id',
    'request_method',
    'request_path',
)


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context: dict[str, Any] = {}

        try:
            from flask import g, has_request_context, request

            if has_request_context():
                context.update({
                    'request_id': getattr(g, 'request_id', ''),
                    'request_method': request.method,
                    'request_path': request.path,
                })
                user_context = getattr(g, 'user_context', None)
                if user_context is not None:
                    context['username'] = getattr(user_context, 'username', '')
        except Exception:
            pass

        runtime_context = _LOG_CONTEXT.get({})
        if runtime_context:
            context.update(runtime_context)

        for field in _LOG_FIELDS:
            value = context.get(field, '')
            setattr(record, field, value if value not in (None, '') else '-')
        return True


class _ErrorOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


class Logger:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._init_logger()
        return cls._instance

    def _init_logger(self):
        self.logger = logging.getLogger('data_insight')
        if getattr(self.logger, '_data_insight_initialized', False):
            return

        log_level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
        self.logger.setLevel(log_level)
        self.logger.propagate = False
        self.logger.handlers.clear()

        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s %(name)s '
            '[%(filename)s:%(lineno)d %(funcName)s] '
            '[request_id=%(request_id)s user=%(username)s ns=%(namespace_id)s '
            'conv=%(conversation_id)s turn=%(turn_id)s exec=%(execution_id)s] '
            '%(message)s'
        )

        context_filter = _RequestContextFilter()

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(log_level)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(context_filter)
        self.logger.addHandler(stream_handler)

        if Config.LOG_ENABLE_FILE:
            log_dir = Path(Config.LOG_DIR)
            if not log_dir.is_absolute():
                project_root = Path(__file__).resolve().parents[2]
                log_dir = project_root / log_dir
            log_dir.mkdir(parents=True, exist_ok=True)

            app_handler = TimedRotatingFileHandler(
                filename=os.fspath(log_dir / 'app.log'),
                when='midnight',
                interval=1,
                backupCount=Config.LOG_RETENTION_DAYS,
                encoding='utf-8',
            )
            app_handler.setLevel(log_level)
            app_handler.setFormatter(formatter)
            app_handler.addFilter(context_filter)
            self.logger.addHandler(app_handler)

            error_handler = TimedRotatingFileHandler(
                filename=os.fspath(log_dir / 'error.log'),
                when='midnight',
                interval=1,
                backupCount=Config.LOG_RETENTION_DAYS,
                encoding='utf-8',
            )
            error_handler.setLevel(logging.WARNING)
            error_handler.setFormatter(formatter)
            error_handler.addFilter(context_filter)
            error_handler.addFilter(_ErrorOnlyFilter())
            self.logger.addHandler(error_handler)

        self.logger._data_insight_initialized = True

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

    def _log(self, level: str, message: str, *args, **kwargs):
        stacklevel = int(kwargs.pop('stacklevel', 2) or 2)
        log_method = getattr(self.logger, level)
        log_method(self._format_message(message, *args), stacklevel=stacklevel, **kwargs)

    def bind_context(self, **kwargs) -> Token:
        current = dict(_LOG_CONTEXT.get({}))
        current.update({
            key: value
            for key, value in kwargs.items()
            if value not in (None, '')
        })
        return _LOG_CONTEXT.set(current)

    def reset_context(self, token: Token) -> None:
        _LOG_CONTEXT.reset(token)

    @contextmanager
    def context(self, **kwargs):
        token = self.bind_context(**kwargs)
        try:
            yield
        finally:
            self.reset_context(token)

    def debug(self, message: str, *args, **kwargs):
        kwargs.setdefault('stacklevel', 3)
        self._log('debug', message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        kwargs.setdefault('stacklevel', 3)
        self._log('info', message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        kwargs.setdefault('stacklevel', 3)
        self._log('warning', message, *args, **kwargs)

    def warn(self, message: str, *args, **kwargs):
        kwargs.setdefault('stacklevel', 4)
        self.warning(message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        kwargs.setdefault('stacklevel', 3)
        self._log('error', message, *args, **kwargs)

    def critical(self, message: str, *args, **kwargs):
        kwargs.setdefault('stacklevel', 3)
        self._log('critical', message, *args, **kwargs)


logger = Logger()
