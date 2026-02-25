import logging
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    """Configure structlog with pretty console output."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


# Initialize with default level; main.py can call setup_logging() again with a custom level.
setup_logging()

logger = structlog.get_logger()

# Route uncaught exceptions through structlog
_original_excepthook = sys.excepthook


def _excepthook(exc_type, exc_value, exc_tb):
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.exit(1)


sys.excepthook = _excepthook
