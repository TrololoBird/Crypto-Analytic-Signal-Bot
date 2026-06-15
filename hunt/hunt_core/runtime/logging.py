"""Script helpers for hunt entrypoints — keeps hunt independent of repo-level scripts/."""
from __future__ import annotations



import logging

import structlog


def configure_script_logging(name: str) -> structlog.BoundLogger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        force=True,
    )
    return structlog.get_logger(name)


__all__ = ["configure_script_logging"]
