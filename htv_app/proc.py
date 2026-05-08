"""Build a snapshot of running harness processes keyed by cwd.

Active-detection for claude/pi (which don't write lock files) needs:
  "is there a live kiro-cli / claude / pi process whose cwd is <X>?"

We answer this by walking /proc/<pid>/{cwd,comm,cmdline} once per refresh.
Only Linux (/proc) is supported; on macOS we'd need lsof fallback — out of scope for v1.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Process comms we care about. Extend via config later if needed.
DEFAULT_COMMS: tuple[str, ...] = ("kiro-cli", "claude", "pi", "node", "codex")


@dataclass
class ProcEntry:
    pid: int
    comm: str           # kernel-truncated name from /proc/<pid>/comm
    cwd: str            # resolved /proc/<pid>/cwd (may be "" if unreadable)
    cmdline: str = ""   # joined /proc/<pid>/cmdline, for harness disambiguation (node → claude?)


class ProcIndex:
    """Snapshot of harness-relevant processes. Cheap to build (~few ms for hundreds of pids)."""

    def __init__(self, comms: tuple[str, ...] = DEFAULT_COMMS):
        self._by_cwd: dict[str, list[ProcEntry]] = {}
        self._by_pid: dict[int, ProcEntry] = {}
        self._scan(comms)

    def _scan(self, comms: tuple[str, ...]) -> None:
        if not os.path.isdir("/proc"):
            return
        comm_set = set(comms)
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                continue
            if comm not in comm_set:
                continue
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            cmdline = ""
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    raw = f.read()
                cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            except OSError:
                pass
            pe = ProcEntry(pid=pid, comm=comm, cwd=cwd, cmdline=cmdline)
            self._by_pid[pid] = pe
            if cwd:
                self._by_cwd.setdefault(cwd, []).append(pe)

    # ---- Queries ----

    def processes_in_cwd(self, cwd: str, comms: Optional[tuple[str, ...]] = None) -> list[ProcEntry]:
        """Return processes whose cwd matches, optionally filtered to specific comms."""
        hits = self._by_cwd.get(cwd, [])
        if comms is None:
            return list(hits)
        cs = set(comms)
        return [p for p in hits if p.comm in cs]

    def get_pid(self, pid: int) -> Optional[ProcEntry]:
        return self._by_pid.get(pid)

    def all(self) -> list[ProcEntry]:
        return list(self._by_pid.values())

    def __repr__(self) -> str:
        return f"ProcIndex(pids={len(self._by_pid)}, cwds={len(self._by_cwd)})"


__all__ = ["ProcEntry", "ProcIndex", "DEFAULT_COMMS"]
