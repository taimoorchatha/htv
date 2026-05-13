"""Tiny mtime+size-keyed memoization for per-jsonl counts.

The refresh tick used to re-parse every jsonl on disk every 2s. For a user
with 161 claude sessions and a few 16 MB conversation files, that's ~3.5s of
blocking work per tick. This cache turns the steady state into a single
stat() per file (the counter only runs again when mtime_ns or size changes).

Single global cache shared across adapter instances. Process-lifetime only —
we don't persist to disk because warming on startup is cheap (one full pass).
"""
from __future__ import annotations

import os
from typing import Callable

# (path, mtime_ns, size) -> count
_count_cache: dict[tuple[str, int, int], int] = {}


def cached_count(path: str, counter: Callable[[str], int]) -> int:
    """Return `counter(path)`, memoized on (path, mtime_ns, size).

    If the file is missing/unreadable, returns 0 and does not cache.
    """
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _count_cache:
        return _count_cache[key]
    n = counter(path)
    _count_cache[key] = n
    # Cap cache size to keep memory in check on long-running dashboards. 4096
    # entries × ~80 bytes ≈ 350 KB worst case. We drop the oldest insertion
    # (Python 3.7+ preserves dict insertion order).
    if len(_count_cache) > 4096:
        oldest = next(iter(_count_cache))
        _count_cache.pop(oldest, None)
    return n


def clear() -> None:
    """Test hook — drop the cache."""
    _count_cache.clear()


__all__ = ["cached_count", "clear"]
