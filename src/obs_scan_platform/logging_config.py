import logging
import sys
from pathlib import Path


class _DropHttpRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("httpx", "httpcore")) and record.levelno < logging.WARNING:
            return False
        return True


def _add_filter_once(target: logging.Handler | logging.Logger, filter_type: type[logging.Filter]) -> None:
    if not any(isinstance(existing_filter, filter_type) for existing_filter in target.filters):
        target.addFilter(filter_type())


def configure_logging(log_path: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    for handler in handlers:
        _add_filter_once(handler, _DropHttpRequestLogFilter)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    for logger_name in ("httpx", "httpcore"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.WARNING)
        _add_filter_once(logger, _DropHttpRequestLogFilter)
