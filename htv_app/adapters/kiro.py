"""Kiro adapter — ported from kirotv.

Session store layout:
  ~/.kiro/sessions/cli/
    <sid>.json      — metadata { cwd, title, updated_at }
    <sid>.jsonl     — message log
    <sid>.lock      — JSON { pid } when a kiro-cli is holding the session

Sidecars we write (elsewhere):
    <sid>.htv-title.json    — AI-generated title cache
    <sid>.htv-meta.json     — user name + tags (step 4)
"""
from __future__ import annotations

import glob
import json
import os

from .base import Adapter, register
from ..proc import ProcIndex
from ..session import SessionRow
from ._util import count_lines as _count_lines
from ._cache import cached_count


class KiroAdapter(Adapter):
    kind = "kiro"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.session_dir = os.path.expanduser(cfg.extra.get("session_dir", "~/.kiro/sessions/cli"))

    # ------------------------------------------------------------------

    def list_sessions(self, procs: ProcIndex) -> list[SessionRow]:
        rows: list[SessionRow] = []
        if not os.path.isdir(self.session_dir):
            return rows

        for jp in glob.glob(os.path.join(self.session_dir, "*.json")):
            base = os.path.basename(jp)
            # Skip our own sidecar files.
            if base.endswith(".htv-title.json") or base.endswith(".htv-meta.json") or base.endswith(".kirotv-title.json"):
                continue
            sid = base[:-5]
            try:
                with open(jp) as f:
                    meta = json.load(f)
            except Exception:
                continue

            # Liveness via .lock file
            lock_path = os.path.join(self.session_dir, f"{sid}.lock")
            active, pid = False, None
            if os.path.exists(lock_path):
                try:
                    with open(lock_path) as lf:
                        pid = json.load(lf).get("pid")
                    if pid:
                        os.kill(pid, 0)  # liveness check
                        active = True
                except (OSError, ValueError, json.JSONDecodeError):
                    active, pid = False, None  # stale lock

            jsonl = os.path.join(self.session_dir, f"{sid}.jsonl")
            msgs = cached_count(jsonl, _count_lines)

            rows.append(SessionRow(
                harness=self.name,
                sid=sid,
                jsonl=jsonl,
                cwd=meta.get("cwd", "?"),
                title=(meta.get("title") or "").strip(),
                updated=meta.get("updated_at", ""),
                msgs=msgs,
                active=active,
                pid=pid if active else None,
                extra={"meta": meta},
            ))
        return rows

    # ------------------------------------------------------------------

    def tail_entries(self, row: SessionRow, n: int = 10000) -> list[tuple[str, str]]:
        return _parse_kiro_jsonl(row.jsonl, n)


# Kind label for rendering, like kirotv's
_KIND_LABEL = {
    "Prompt": "USER",
    "AssistantMessage": "AI",
    "ToolResults": "TOOL",
}


def _parse_kiro_jsonl(path: str, n: int) -> list[tuple[str, str]]:
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
        kind = obj.get("kind", "?")
        data = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
        preview = ""
        if kind == "Prompt":
            c = data.get("content", [])
            if c and isinstance(c[0], dict):
                preview = c[0].get("data", "") or ""
        elif kind == "AssistantMessage":
            for item in (data.get("content") or [])[:6]:
                if not isinstance(item, dict):
                    continue
                k = item.get("kind")
                d = item.get("data", {})
                if k == "text":
                    preview += (d if isinstance(d, str) else str(d)) + " "
                elif k == "tool_use":
                    name = d.get("name", "?") if isinstance(d, dict) else "?"
                    preview += f"[🔧 {name}] "
                elif k == "thinking":
                    preview += "[💭 thinking] "
        elif kind == "ToolResults":
            items = data.get("content") or data.get("tool_uses") or []
            preview = f"{len(items)} result(s)" if items else "results"
        else:
            preview = json.dumps(data)[:120] if data else ""
        preview = preview.replace("\n", " ⏎ ").strip() or "(empty)"
        label = _KIND_LABEL.get(kind, kind[:10])
        out.append((label, preview))
    return out


register("kiro", KiroAdapter)
