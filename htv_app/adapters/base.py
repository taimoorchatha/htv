"""Adapter base class + registry.

Each harness (kiro, claude, pi, …) implements this interface to expose its sessions
to htv. Adapters are instantiated with a HarnessConfig at startup.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

from ..config import HarnessConfig
from ..proc import ProcIndex
from ..session import SessionRow


class Adapter(ABC):
    """A harness adapter. Lifecycle: instantiate once with config, reused across refreshes."""

    kind: str = ""  # subclass sets this ("kiro" | "claude" | "pi" | ...)

    def __init__(self, cfg: HarnessConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.label = cfg.label
        self.color = cfg.color

    # ---- Required ----

    @abstractmethod
    def list_sessions(self, procs: ProcIndex) -> list[SessionRow]:
        """Scan the store and return all sessions with active/pid populated."""
        ...

    @abstractmethod
    def tail_entries(self, row: SessionRow, n: int = 10000) -> list[tuple[str, str]]:
        """Parse the jsonl into (kind_label, preview_string) pairs for rendering.
        kind_label is one of 'USER' / 'AI' / 'TOOL' / 'SYS' / ...
        """
        ...

    # ---- Optional hooks ----

    def resume_argv(self, row: SessionRow) -> list[str]:
        """Interpolate {sid}, {cwd}, {jsonl} into resume_cmd from config.

        If `resume_via_shell` is set on the harness, wrap the result as
        `$SHELL -i -c 'exec <quoted argv>'` so version managers (nvm, asdf,
        mise, pyenv, rbenv) and shell aliases get a chance to fire before
        the harness binary is resolved.
        """
        placeholders = {
            "sid": row.sid,
            "cwd": row.cwd,
            "jsonl": row.jsonl,
        }
        argv = [s.format(**placeholders) for s in self.cfg.resume_cmd]
        if not argv or not getattr(self.cfg, "resume_via_shell", False):
            return argv
        shell = os.environ.get("SHELL", "/bin/sh")
        inner = "exec " + " ".join(_shell_quote(a) for a in argv)
        return [shell, "-i", "-c", inner]

    def __repr__(self) -> str:
        return f"<Adapter {self.name} kind={self.kind}>"


class AdapterError(RuntimeError):
    pass


# ---- Registry ----

_REGISTRY: dict[str, type[Adapter]] = {}


def register(kind: str, cls: type[Adapter]) -> None:
    _REGISTRY[kind] = cls


def get_adapter_cls(kind: str) -> type[Adapter] | None:
    return _REGISTRY.get(kind)


def all_kinds() -> list[str]:
    return sorted(_REGISTRY.keys())


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting — single-quote-safe. Duplicated from tmux_util
    so the adapter layer doesn't depend on tmux helpers."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "_-./=@+:{}" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


__all__ = ["Adapter", "AdapterError", "register", "get_adapter_cls", "all_kinds"]
