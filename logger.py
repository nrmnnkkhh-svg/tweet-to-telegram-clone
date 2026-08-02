#!/usr/bin/env python3
import contextvars, logging, logging.handlers, os, queue, sys, time
from pathlib import Path
from typing import Optional

FEATURE_LOGGING: bool = True
INSTANCE         = os.environ.get("BOT_INSTANCE", "main")
LOG_FILE         = Path(f"{INSTANCE}_bot.log")
LOG_MAX_BYTES    = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 1

_FMT = (
    "%(asctime)s"
    " | %(levelname)-8s"
    " | %(section)-20s"
    " | %(tweet_id)-22s"
    " | %(message)s"
)
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

_ctx_section:  contextvars.ContextVar[str] = contextvars.ContextVar("section",  default="-")
_ctx_tweet_id: contextvars.ContextVar[str] = contextvars.ContextVar("tweet_id", default="-")

def set_log_context(section: Optional[str] = None, tweet_id: Optional[str] = None) -> None:
    if section  is not None: _ctx_section.set(str(section))
    if tweet_id is not None: _ctx_tweet_id.set(str(tweet_id))

def clear_log_context() -> None:
    _ctx_section.set("-")
    _ctx_tweet_id.set("-")

class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.section  = getattr(record, "section",  None) or _ctx_section.get()
        record.tweet_id = getattr(record, "tweet_id", None) or _ctx_tweet_id.get()
        return True

class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

_log_queue:   queue.Queue                              = queue.Queue(-1)
_listener:    Optional[logging.handlers.QueueListener] = None
_root_logger: Optional[logging.Logger]                = None

def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    global _listener, _root_logger
    if _root_logger is not None:
        return _root_logger

    logger = logging.getLogger("bot")
    logger.setLevel(level)
    logger.propagate = False

    if not FEATURE_LOGGING:
        logger.addHandler(logging.NullHandler())
        _root_logger = logger
        return logger

    formatter  = _UTCFormatter(_FMT, datefmt=_DATE_FMT)
    ctx_filter = _ContextFilter()

    q_handler = logging.handlers.QueueHandler(_log_queue)
    q_handler.addFilter(ctx_filter)
    logger.addHandler(q_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)

    file_h = logging.handlers.RotatingFileHandler(
        str(LOG_FILE), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8", delay=False
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(formatter)

    _listener = logging.handlers.QueueListener(_log_queue, console, file_h, respect_handler_level=True)
    _listener.start()

    _root_logger = logger
    logger.info("━" * 56, extra={"section": "logger", "tweet_id": "-"})
    logger.info(f"Logging started | instance={INSTANCE!r} | file={LOG_FILE} | flag={FEATURE_LOGGING}",
                extra={"section": "logger", "tweet_id": "-"})
    return logger

def get_logger(section: str = "main") -> logging.Logger:
    if _root_logger is None:
        setup_logging()
    return _root_logger.getChild(section)

def log_exception(logger: logging.Logger, exc: BaseException, msg: str = "Exception caught") -> None:
    logger.error(msg, exc_info=exc)

def flush_and_stop() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None
