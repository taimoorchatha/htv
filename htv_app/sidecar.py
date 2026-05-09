"""Per-session sidecar metadata.

htv owns `<base>.htv-meta.json` files that live alongside each harness's
JSONL session log. We never touch upstream session files — only read them.

Sidecar format:
    {
        "name": "fix cagg timeout",
        "tags": ["oncall", "s2p"],
        "updated_at": "2026-05-09T07:30:00Z"
    }

Path mapping:
    <dir>/<base>.jsonl  →  <dir>/<base>.htv-meta.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def sidecar_path(jsonl_path: str) -> str:
    """Derive the sidecar path from a jsonl path (swap extension)."""
    if jsonl_path.endswith(".jsonl"):
        return jsonl_path[:-6] + ".htv-meta.json"
    return jsonl_path + ".htv-meta.json"


def load(jsonl_path: str) -> dict:
    """Return {"name": str, "tags": list[str]} for a session. Empty dict if no sidecar
    or any read/parse error — sidecars are best-effort by design."""
    p = sidecar_path(jsonl_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {"name": str(data.get("name", "")), "tags": [str(t) for t in tags]}


def save(jsonl_path: str, *, name: str, tags: list[str]) -> None:
    """Write {name, tags, updated_at} next to the jsonl. Raises OSError on failure."""
    p = sidecar_path(jsonl_path)
    payload = {
        "name": name.strip(),
        "tags": [t.strip() for t in tags if t.strip()],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    # Atomic-ish: write to temp then rename, so readers never see a half-written file.
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, p)


__all__ = ["sidecar_path", "load", "save"]
