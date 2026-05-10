"""
JSON log setup for Fly's log aggregator.

Every process calls configure() once at startup. After that, the
existing print(flush=True) calls inside service code keep working
unchanged — they go to stdout, Fly captures them as plain text. The
HTTP and bot loggers go through this configured handler, so their
records become structured JSON with user_id / route / status fields.
"""

import logging

from pythonjsonlogger import jsonlogger


def configure(level: str = "INFO") -> None:
    """
    1. Build a StreamHandler that emits JSON via python-json-logger.
    2. Replace the root logger's handlers so every logging.getLogger()
       child inherits structured output.
    3. Set the requested log level on root.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
