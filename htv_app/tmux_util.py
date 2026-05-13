"""tmux helpers for smart-attach.

Answers the question: "is this session's process living in a tmux pane,
and if so which one?" — by walking the process's /proc ancestor chain
and matching against `tmux list-panes`.

All heavy lifting stays off the UI thread's critical path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional


def _sh(cmd: list[str], timeout: int = 3) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def have_tmux() -> bool:
    """True iff the `tmux` binary is on PATH. Used to gracefully degrade the `t` keybinding."""
    return shutil.which("tmux") is not None


def _ppid(pid: int) -> Optional[int]:
    """Parent pid of `pid`. Linux: /proc/<pid>/status. macOS/BSD: `ps -o ppid=`."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status") as f:
                for ln in f:
                    if ln.startswith("PPid:"):
                        return int(ln.split()[1])
        except OSError:
            return None
        return None
    # macOS / BSD fallback.
    out = _sh(["ps", "-o", "ppid=", "-p", str(pid)]).strip()
    if not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def ancestor_chain(pid: int, max_depth: int = 12) -> list[int]:
    """Return [pid, ppid, ..., init]."""
    out: list[int] = []
    cur: Optional[int] = pid
    while cur and len(out) < max_depth:
        out.append(cur)
        nxt = _ppid(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return out


def find_tmux_pane(pid: int) -> Optional[str]:
    """Return 'session:win.pane' if `pid`'s ancestor chain hits a tmux pane, else None."""
    if not pid or not have_tmux():
        return None
    chain = set(ancestor_chain(pid))
    if not chain:
        return None
    panes = _sh(["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}:#{window_index}.#{pane_index}"])
    for line in panes.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pane_pid = int(parts[0])
        except ValueError:
            continue
        if pane_pid in chain:
            return parts[1]
    return None


def tty_of(pid: int) -> str:
    return _sh(["ps", "-o", "tty=", "-p", str(pid)]).strip()


def find_kitty_window(pid: int) -> str:
    """Return the kitty window id hosting `pid` (walks through kitty's foreground_processes).

    Empty string if kitten can't be reached, kitty remote control is off,
    or no window's process tree contains `pid`'s ancestor chain.
    Why we walk ancestors: `kitten @ ls` only reports top-level foreground
    processes (the shell), not our grandchild harness binary.
    """
    if not pid or not __import__("shutil").which("kitten"):
        return ""
    chain = set(ancestor_chain(pid))
    if not chain:
        return ""
    out = _sh(["kitten", "@", "ls"])
    if not out:
        return ""
    try:
        import json
        groups = json.loads(out)
    except Exception:
        return ""
    for group in groups:
        for tab in group.get("tabs", []):
            for w in tab.get("windows", []):
                for proc in w.get("foreground_processes", []):
                    if proc.get("pid") in chain:
                        return str(w.get("id") or "")
    return ""


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def create_session(name: str, cwd: str, argv: list[str]) -> tuple[bool, str]:
    """Create a detached tmux session `name` running `argv` in `cwd`.
    Returns (ok, message-or-error)."""
    if not have_tmux():
        return False, "tmux not installed (try: brew install tmux  /  apt install tmux)"
    if subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0:
        return False, f"tmux session '{name}' already exists"
    # Use sh -c to inject a cd step; exec so the resumed process replaces the shell.
    cmd = "cd {cwd!r} 2>/dev/null; exec {argv}".format(
        cwd=cwd,
        argv=" ".join(_shell_quote(a) for a in argv),
    )
    r = subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-n", "chat", "sh", "-c", cmd],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, (r.stderr or "tmux failed").strip()[:120]
    return True, name


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting — single-quote-safe."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "_-./=@+:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


__all__ = ["find_tmux_pane", "find_kitty_window", "tty_of", "inside_tmux", "create_session", "ancestor_chain", "have_tmux"]
