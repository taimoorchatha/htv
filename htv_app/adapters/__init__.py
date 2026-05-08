"""Adapter registry. Importing this module registers all built-in adapters."""
from __future__ import annotations

from .base import Adapter, AdapterError, register, get_adapter_cls, all_kinds

# Trigger each adapter's self-registration
from . import kiro as _kiro     # noqa: E402,F401
from . import claude as _claude # noqa: E402,F401
from . import pi as _pi         # noqa: E402,F401

__all__ = ["Adapter", "AdapterError", "register", "get_adapter_cls", "all_kinds"]
