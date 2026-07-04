"""Shared, structured logger used across the whole application."""

import json
import logging
import os
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


logger = logging.getLogger("text2sql-secure")
level_name = os.getenv("LOG_LEVEL", "INFO").upper()
logger.setLevel(getattr(logging, level_name, logging.INFO))

handler = logging.StreamHandler(sys.stdout)
formatter = JsonFormatter()
if os.getenv("LOG_FORMAT", "json").lower() != "json":
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(handler)
