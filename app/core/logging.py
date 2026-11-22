"""Simple structured (key=value) logging setup."""
import logging
import sys

from app.core.config import settings


def setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stdout,
        format="%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
