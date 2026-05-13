"""Build a snapshot of running harness processes keyed by cwd.

Active-detection for claude/pi (which don't write lock files) needs:
  "is there a live kiro-cli / claude / pi process whose cwd is <X>?"

Linux: walk /proc/<pid>/{cwd,comm,cmdline} once per refresh — fast, no forks.
macOS: /proc doesn't exist. We use `ps` to discover PIDs by comm, then a
       single batched `lsof -p p1,p2,... -d cwd` call to read cwds.
       ~300ms total for typical machines vs 2-3s if we forked once per pid.
Other: returns an empty index (active detection silently disabled).
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

# Process comms we care about. Extend via config later if needed.
DEFAULT_COMMS: tuple[str, ...] = ("kiro-cli", "claude", "pi", "node", "codex")


@dataclass
class ProcEntry:
    pid: int
    comm: str           # process basename (e.g. "claude")
    cwd: str            # current working directory (may be "" if unreadable)
    cmdline: str = ""   # joined command line, for harness disambiguation (node → claude?)


class ProcIndex:
    """Snapshot of harness-relevant processes. Cheap to build (~ms on Linux, ~300ms on macOS)."""

    def __init__(self, comms: tuple[str, ...] = DEFAULT_COMMS):
        self._by_cwd: dict[str, list[ProcEntry]] = {}
        self._by_pid: dict[int, ProcEntry] = {}
        if sys.platform.startswith("linux"):
            self._scan_linux(comms)
        elif sys.platform == "darwin":
            self._scan_darwin(comms)
        # Other platforms: empty index, active detection silently no-ops.

    # ---- Linux: /proc walk ----

    def _scan_linux(self, comms: tuple[str, ...]) -> None:
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
            self._record(ProcEntry(pid=pid, comm=comm, cwd=cwd, cmdline=cmdline))

    # ---- macOS: ps + batched lsof ----

    def _scan_darwin(self, comms: tuple[str, ...]) -> None:
        comm_set = set(comms)
        # `ps -axo pid=,comm=` — we skip `command=` because it can be huge (claude argv
        # is multi-KB) and we don't use cmdline on macOS. comm = executable basename.
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,comm="],
                capture_output=True, text=True, timeout=3,
            ).stdout
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return

        candidates: dict[int, str] = {}  # pid -> comm
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            # `comm` from ps may be a full path on macOS — take the basename.
            comm = os.path.basename(parts[1])
            if comm in comm_set:
                candidates[pid] = comm

        if not candidates:
            return

        cwds = self._lsof_cwds(list(candidates.keys()))
        for pid, comm in candidates.items():
            self._record(ProcEntry(pid=pid, comm=comm, cwd=cwds.get(pid, "")))

    @staticmethod
    def _lsof_cwds(pids: list[int]) -> dict[int, str]:
        """Single batched lsof call: pid -> cwd. Empty dict on failure."""
        if not pids:
            return {}
        # -F pn → field-mode output: 'p<pid>' then 'n<path>' lines per record.
        # -a -d cwd → AND filter to only the cwd file descriptor.
        # -p p1,p2,... → comma-separated pid list (one fork instead of N).
        try:
            r = subprocess.run(
                ["lsof", "-p", ",".join(str(p) for p in pids), "-a", "-d", "cwd", "-F", "pn"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return {}
        out: dict[int, str] = {}
        cur_pid: Optional[int] = None
        for line in r.stdout.splitlines():
            if not line:
                continue
            tag, val = line[0], line[1:]
            if tag == "p":
                try:
                    cur_pid = int(val)
                except ValueError:
                    cur_pid = None
            elif tag == "n" and cur_pid is not None:
                out[cur_pid] = val
        return out

    # ---- Internal ----

    def _record(self, pe: ProcEntry) -> None:
        self._by_pid[pe.pid] = pe
        if pe.cwd:
            self._by_cwd.setdefault(pe.cwd, []).append(pe)

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
