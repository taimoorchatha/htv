"""Claude Code adapter.

Session store layout:
  ~/.claude/projects/
    <encoded-cwd>/
      <sid>.jsonl             — message log (one line per message/event)
      memory/                  — ignored
      <parent-sid>/subagents/  — sidechain logs, ignored (we only list top-level)

Claude's JSONL interleaves conversation turns with metadata events
(permission-mode, file-history-snapshot, custom-title, etc.). We filter
those out of the tail view and out of the msgs count.
"""
from __future__ import annotations

import glob
import json
import os
from .base import Adapter, register
from ..proc import ProcIndex
from ..session import SessionRow
from ._util import iso_from_mtime as _iso_from_mtime
from ._cache import cached_count

# Max length for a title snippet pulled from the jsonl.
_TITLE_LEN = 80

# Non-conversation event types Claude writes. Filtered from tail + msgs.
_META_EVENT_TYPES = frozenset({
    "permission-mode", "file-history-snapshot", "last-prompt", "custom-title",
    "agent-name", "compact", "compaction-summary", "thinking-level-change",
    "model-change", "tool-use-id-map", "session-metadata", "usage",
})


def _is_conversation_event(obj: dict) -> bool:
    """True if this JSONL row is a real conversation turn."""
    typ = obj.get("type")
    if typ in _META_EVENT_TYPES:
        return False
    if obj.get("isMeta"):
        return False
    if obj.get("isSidechain"):
        return False
    return typ in ("user", "assistant", "system", "summary")


class ClaudeAdapter(Adapter):
    kind = "claude"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.projects_dir = os.path.expanduser(cfg.extra.get("projects_dir", "~/.claude/projects"))
        self.active_window = int(cfg.extra.get("active_mtime_window_sec", 90))

    def list_sessions(self, procs: ProcIndex) -> list[SessionRow]:
        rows: list[SessionRow] = []
        if not os.path.isdir(self.projects_dir):
            return rows

        # Pass 1: build all rows; remember (cwd, mtime) so we can pick the
        # "current" jsonl per cwd in pass 2.
        by_cwd: dict[str, list[tuple[float, SessionRow]]] = {}
        for proj in sorted(os.listdir(self.projects_dir)):
            proj_path = os.path.join(self.projects_dir, proj)
            if not os.path.isdir(proj_path):
                continue
            for jsonl in glob.glob(os.path.join(proj_path, "*.jsonl")):
                sid = os.path.splitext(os.path.basename(jsonl))[0]
                cwd_entry, first_user = _peek_jsonl(jsonl)
                cwd = (cwd_entry or {}).get("cwd") or _decode_dashed_dir(proj)
                try:
                    mtime = os.path.getmtime(jsonl)
                except OSError:
                    mtime = 0

                title = _extract_text(first_user) if first_user else ""
                row = SessionRow(
                    harness=self.name,
                    sid=sid,
                    jsonl=jsonl,
                    cwd=cwd or "?",
                    title=title[:_TITLE_LEN],
                    updated=_iso_from_mtime(mtime),
                    msgs=cached_count(jsonl, _count_conversation),
                    active=False,
                    pid=None,
                    extra={"project_dir": proj_path},
                )
                rows.append(row)
                if cwd:
                    by_cwd.setdefault(cwd, []).append((mtime, row))

        # Pass 2: for each cwd with a live `claude` process, mark the most-recently
        # modified jsonl in that cwd as active. This handles two facts at once:
        #   1) a live process at a prompt has stale mtime (was: incorrectly idle)
        #   2) one cwd can have many old jsonls; only the newest is the current one
        for cwd, entries in by_cwd.items():
            live = procs.processes_in_cwd(cwd, comms=("claude",))
            if not live:
                continue
            _, newest = max(entries, key=lambda x: x[0])
            newest.active = True
            newest.pid = live[0].pid

        return rows

    def tail_entries(self, row: SessionRow, n: int = 10000) -> list[tuple[str, str]]:
        return _parse_claude_jsonl(row.jsonl, n)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _count_conversation(path: str) -> int:
    """Count only real user/assistant turns (exclude metadata events)."""
    if not os.path.exists(path):
        return 0
    n = 0
    try:
        with open(path) as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if _is_conversation_event(obj) and obj.get("type") in ("user", "assistant"):
                    n += 1
    except OSError:
        pass
    return n


def _decode_dashed_dir(name: str) -> str:
    """Lossy fallback: claude encodes `/` and `.` as `-`. Only used if the jsonl
    has no `cwd` field (rare)."""
    if name.startswith("-"):
        return name.replace("-", "/")
    return name


def _peek_jsonl(path: str) -> tuple[dict | None, dict | None]:
    """Return (first_entry_with_cwd, first_real_user_prompt).

    Scans up to 100 lines. Stops early once both are found. Skips tool_result
    echoes and <command-*>/<local-command-*> internal markers when picking the
    first user prompt.
    """
    if not os.path.exists(path):
        return None, None
    cwd_entry: dict | None = None
    first_user: dict | None = None
    try:
        with open(path) as f:
            for i, ln in enumerate(f):
                if i > 100:
                    break
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if cwd_entry is None and obj.get("cwd"):
                    cwd_entry = obj
                if first_user is None and obj.get("type") == "user" and not obj.get("isMeta") and not obj.get("isSidechain"):
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    text = _extract_text({"message": {"content": content}})
                    if text and _is_real_prompt(content, text):
                        first_user = obj
                if cwd_entry is not None and first_user is not None:
                    break
    except OSError:
        return cwd_entry, first_user
    return cwd_entry, first_user


def _is_real_prompt(content, text: str) -> bool:
    """True if this user message looks like a real human prompt (not a tool_result
    echo and not a <command-*>/<local-command-*> internal marker)."""
    if isinstance(content, list):
        if content and all(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
            return False
    if text.startswith("<command-") or text.startswith("<local-command-"):
        return False
    return True


def _extract_text(entry: dict | None) -> str:
    """Plain-text preview from a claude message entry."""
    if not entry:
        return ""
    msg = entry.get("message") or entry
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content[:6]:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(str(item.get("text", "")))
            elif t == "tool_use":
                parts.append(f"[🔧 {item.get('name', '?')}]")
            elif t == "tool_result":
                body = item.get("content")
                if isinstance(body, str):
                    parts.append(f"[→ {body[:80]}]")
                else:
                    parts.append("[→ result]")
            elif t == "thinking":
                parts.append("[💭 thinking]")
        return " ".join(parts)
    return str(content) if content is not None else ""


def _parse_claude_jsonl(path: str, n: int) -> list[tuple[str, str]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if not _is_conversation_event(obj):
            continue
        typ = obj.get("type")
        msg = obj.get("message") or {}
        role = msg.get("role") or typ
        if typ in ("user", "assistant"):
            label = "USER" if role == "user" else "AI"
        elif typ == "system":
            label = "SYS"
        elif typ == "summary":
            label = "SUM"
        else:
            label = (typ or "?")[:10].upper()
        preview = _extract_text(obj).replace("\n", " ⏎ ").strip()
        if not preview:
            preview = "(empty)"
        out.append((label, preview))
    return out


register("claude", ClaudeAdapter)
