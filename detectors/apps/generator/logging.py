"""Opt-in JSON lines logging; importing the library never configures root logging."""

from datetime import datetime, timezone
import json
import logging
from typing import TextIO


LOGGER_NAME = 'apps.generator'
logger = logging.getLogger(LOGGER_NAME)
logger.addHandler(logging.NullHandler())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'event': getattr(record, 'event', record.getMessage()),
            'message': record.getMessage(),
            'details': getattr(record, 'details', {}),
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def configure_json_logging(*, level: int = logging.INFO,
                           stream: TextIO | None = None) -> logging.Logger:
    """Configure only this package. Repeated calls replace the owned handler."""
    for handler in tuple(logger.handlers):
        if getattr(handler, '_generator_json', False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream)
    handler._generator_json = True
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
