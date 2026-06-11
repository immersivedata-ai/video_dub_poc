import logging
import sys
from datetime import datetime, timezone

_logger = None


def get_logger(name: str = "dubbing-studio") -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger.getChild(name)

    _logger = logging.getLogger("dubbing-studio")
    _logger.setLevel(logging.DEBUG)

    if not _logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
            datefmt="%H:%M:%S"
        )
        handler.setFormatter(fmt)
        _logger.addHandler(handler)

    return _logger.getChild(name)
