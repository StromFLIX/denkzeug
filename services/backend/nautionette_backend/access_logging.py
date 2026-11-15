"""Access logs never contain query credentials (webhook URLs, OAuth callbacks)."""

from __future__ import annotations

import logging


class SafeAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, version, status = record.args
            record.args = (client, method, str(target).split("?", 1)[0], version, status)
        return True


def install() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SafeAccessFilter) for item in logger.filters):
        logger.addFilter(SafeAccessFilter())
