"""Common data model for a harness session."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionRow:
    """One row in the session list. Populated by an adapter's list_sessions()."""

    harness: str               # 'kiro' | 'claude' | 'pi' | ...
    sid: str                   # session ID (UUID, usually)
    jsonl: str                 # absolute path to the session's JSONL log

    cwd: str = "?"             # working directory of the session
    title: str = ""            # raw/derived title from upstream (or first prompt snippet)
    updated: str = ""          # ISO-8601 timestamp of latest activity (mtime of jsonl)
    msgs: int = 0              # message count

    active: bool = False       # is a live process currently holding this session?
    pid: Optional[int] = None  # pid of that process, if known

    # htv sidecar metadata (populated by config.load_meta)
    name: str = ""             # user-assigned name
    tags: list[str] = field(default_factory=list)
    ai_title: Optional[str] = None  # cached AI-generated title

    # Adapter-specific bag (e.g. kiro puts the raw meta dict here)
    extra: dict = field(default_factory=dict)

    # Derived
    @property
    def display_title(self) -> str:
        """Pick best label: user name > ai title > raw title > '(no title)'."""
        return self.name.strip() or (self.ai_title or "").strip() or self.title.strip() or "(no title)"

    @property
    def key(self) -> str:
        """Composite key for sidecar indexing, logs, etc."""
        return f"{self.harness}:{self.sid}"
