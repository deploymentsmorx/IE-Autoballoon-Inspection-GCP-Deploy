"""Structured logging helper."""

from __future__ import annotations

import logging
import sys

_LOG = logging.getLogger("smorx")
if not _LOG.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)


def get_logger(name: str | None = None) -> logging.Logger:
    if name:
        return _LOG.getChild(name)
    return _LOG
