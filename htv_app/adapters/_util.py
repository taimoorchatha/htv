"""Shared helpers used by multiple adapters.

Kept intentionally small — anything more complex stays in each adapter since
session-file formats diverge. Only exact-duplicate bodies belong here.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


def count_lines(path: str) -> int:
    """Byte-level line count of `path`. 0 on any OS error or missing file."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def iso_from_mtime(mtime: float) -> str:
    """Render a POSIX mtime as a UTC ISO-8601 string like 2026-05-08T15:39:05Z.
    Empty string if mtime is falsy (e.g. 0)."""
    if not mtime:
        return ""
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["count_lines", "iso_from_mtime"]
