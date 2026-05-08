"""Pi adapter.

Session store layout:
  ~/.pi/agent/sessions/
    <encoded-cwd>/                    — wrapped with --...--, dots preserved
      <ISO-timestamp>_<uuid>.jsonl    — session file

First line of each jsonl is a session record with the canonical cwd:
    {"type": "session", "version": 3, "id": "<uuid>", "timestamp": "...", "cwd": "..."}

Subsequent lines are events of many types; the ones we care about:
  {"type": "message", "message": {"role": "user"|"assistant"|"toolResult",
                                   "content": [ ... ]}}
  Content blocks:
    {"type": "text", "text": "..."}
    {"type": "toolCall", "id": "...", "name": "bash", "arguments": {...}}

Other types (model_change, thinking_level_change, custom, custom_message) are metadata
and are skipped in the tail view.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

from .base import Adapter, register
from ..proc import ProcIndex
from ..session import SessionRow
from ._util import count_lines as _count_lines, iso_from_mtime as _iso_from_mtime

_TITLE_LEN = 80
_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


class PiAdapter(Adapter):
    kind = "pi"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.sessions_dir = os.path.expanduser(cfg.extra.get("sessions_dir", "~/.pi/agent/sessions"))
        self.active_window = int(cfg.extra.get("active_mtime_window_sec", 90))

    def list_sessions(self, procs: ProcIndex) -> list[SessionRow]:
        rows: list[SessionRow] = []
        if not os.path.isdir(self.sessions_dir):
            return rows

        now = time.time()
        for proj in sorted(os.listdir(self.sessions_dir)):
            proj_path = os.path.join(self.sessions_dir, proj)
            if not os.path.isdir(proj_path):
                continue
            for jsonl in glob.glob(os.path.join(proj_path, "*.jsonl")):
                sid = _sid_from_filename(os.path.basename(jsonl))
                if not sid:
                    continue
                first_obj, first_user, first_user_text = _peek_pi_jsonl(jsonl)
                # cwd: prefer the first "session" record; fall back to decoded dir name.
                cwd = ((first_obj or {}).get("cwd")
                       if (first_obj or {}).get("type") == "session"
                       else None)
                if not cwd:
                    cwd = _decode_pi_dir(proj)
                try:
                    mtime = os.path.getmtime(jsonl)
                except OSError:
                    mtime = 0

                active = False
                pid = None
                if cwd and (now - mtime) < self.active_window:
                    for pe in procs.processes_in_cwd(cwd, comms=("pi",)):
                        active = True
                        pid = pe.pid
                        break

                rows.append(SessionRow(
                    harness=self.name,
                    sid=sid,
                    jsonl=jsonl,
                    cwd=cwd or "?",
                    title=(first_user_text or "")[:_TITLE_LEN],
                    updated=_iso_from_mtime(mtime),
                    msgs=_count_lines(jsonl),
                    active=active,
                    pid=pid,
                    extra={"project_dir": proj_path},
                ))
        return rows

    def tail_entries(self, row: SessionRow, n: int = 10000) -> list[tuple[str, str]]:
        return _parse_pi_jsonl(row.jsonl, n)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _sid_from_filename(name: str) -> str:
    """Pi filenames look like: 2026-05-08T15-39-05-932Z_019e083e-114c-732d-ac92-44f844a801a7.jsonl"""
    m = _UUID_RE.search(name)
    return m.group(1) if m else os.path.splitext(name)[0]


def _decode_pi_dir(name: str) -> str:
    """Pi encodes cwd as `--<slash-replaced-path>--`. Dots are preserved.
    Strip leading/trailing '--' then replace '-' with '/'. Still lossy for paths
    that legitimately contain dashes, so this is only a fallback."""
    s = name.strip("-")
    return "/" + s.replace("-", "/") if s else name


def _peek_pi_jsonl(path: str) -> tuple[dict | None, dict | None, str]:
    """Return (first_line_obj, first_user_message, first_user_text_snippet)."""
    if not os.path.exists(path):
        return None, None, ""
    first_obj: dict | None = None
    first_user: dict | None = None
    first_text = ""
    try:
        with open(path) as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if first_obj is None:
                    first_obj = obj
                if first_user is None and obj.get("type") == "message":
                    msg = obj.get("message") or {}
                    if msg.get("role") == "user":
                        text = _extract_pi_text(msg)
                        if text:
                            first_user = obj
                            first_text = text
                if first_obj is not None and first_user is not None:
                    break
    except OSError:
        pass
    return first_obj, first_user, first_text


def _extract_pi_text(msg: dict | None) -> str:
    """Pull a text preview from a pi message dict ({role, content})."""
    if not msg:
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for item in c[:6]:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(str(item.get("text", "")))
            elif t == "toolCall":
                parts.append(f"[🔧 {item.get('name', '?')}]")
            elif t == "tool_result" or t == "toolResult":
                body = item.get("text") or item.get("content")
                if isinstance(body, str):
                    parts.append(f"[→ {body[:80]}]")
                else:
                    parts.append("[→ result]")
            elif t == "thinking":
                parts.append("[💭 thinking]")
        return " ".join(parts)
    return str(c) if c is not None else ""


def _parse_pi_jsonl(path: str, n: int) -> list[tuple[str, str]]:
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
        typ = obj.get("type")
        if typ != "message":
            continue  # skip custom/model_change/thinking_level_change/session records
        msg = obj.get("message") or {}
        role = msg.get("role")
        if role == "user":
            label = "USER"
        elif role == "assistant":
            label = "AI"
        elif role == "toolResult":
            label = "TOOL"
        else:
            label = (role or "?").upper()[:10]
        preview = _extract_pi_text(msg).replace("\n", " ⏎ ").strip()
        if not preview:
            preview = "(empty)"
        out.append((label, preview))
    return out


register("pi", PiAdapter)
