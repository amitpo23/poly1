import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


_BUILTIN_LOG_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in _BUILTIN_LOG_RECORD_KEYS and not k.startswith("_"):
                try:
                    json.dumps(v, default=str)
                    payload[k] = v
                except TypeError:
                    payload[k] = str(v)
        return json.dumps(payload, default=str)


def configure_logging(
    level: Optional[str] = None,
    log_dir: Optional[str] = None,
    json_stdout: bool = True,
) -> None:
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    log_dir = log_dir or os.getenv("LOG_DIR", "./data/logs")
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    json_fmt = JsonFormatter()
    plain_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(log_level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler()
    stream.setFormatter(json_fmt if json_stdout else plain_fmt)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "poly1.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(json_fmt)
    root.addHandler(file_handler)

    # Quiet noisy libraries.
    for lib in ("httpx", "urllib3", "web3", "openai"):
        logging.getLogger(lib).setLevel(logging.WARNING)
