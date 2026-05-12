"""Config loader.

Loads ~/.config/htv/config.toml (XDG) with sensible defaults baked in.
First-run creates the file from DEFAULT_CONFIG_TOML so the user has a template to edit.

Config schema (see config.example.toml for docs):

  [app]
  refresh_interval = 2.0
  ai_title_interval = 3.0
  active_mtime_window_sec = 90

  [ai]
  command = ["pi", "-p"]
  timeout = 45

  [focus]
  command = ["kitten", "@", "focus-window", "--match", "pid:{pid}"]

  [harnesses.<name>]
  kind = "kiro" | "claude" | "pi"
  enabled = true
  # adapter-specific keys (session_dir, projects_dir, sessions_dir)
  resume_cmd = [...]   # template with {sid} {cwd} {jsonl}
  label = "K"          # single-char harness marker
  color = "green"      # curses color name or int
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_PATH = os.path.join(XDG_CONFIG_HOME, "htv", "config.toml")


DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "refresh_interval": 2.0,
        "ai_title_interval": 3.0,
        "active_mtime_window_sec": 90,
    },
    "ai": {
        "command": ["pi", "-p"],
        "timeout": 45,
    },
    "focus": {
        # kitty's remote-control; requires `allow_remote_control yes` in kitty.conf.
        # Leave as empty list to disable and show an info message instead.
        "command": ["kitten", "@", "focus-window", "--match", "id:{win_id}"],
    },
    "harnesses": {
        "kiro": {
            "kind": "kiro",
            "enabled": True,
            "session_dir": "~/.kiro/sessions/cli",
            "resume_cmd": ["kiro-cli", "chat", "--resume-id", "{sid}"],
            "label": "K",
            "color": "green",
        },
        "claude": {
            "kind": "claude",
            "enabled": True,
            "projects_dir": "~/.claude/projects",
            "resume_cmd": ["claude", "--resume", "{sid}"],
            "label": "CC",
            "color": "magenta",
        },
        "pi": {
            "kind": "pi",
            "enabled": True,
            "sessions_dir": "~/.pi/agent/sessions",
            "resume_cmd": ["pi", "--session", "{sid}"],
            "label": "pi",
            "color": "cyan",
        },
    },
}


@dataclass
class HarnessConfig:
    name: str
    kind: str
    enabled: bool = True
    label: str = "?"
    color: str = "white"
    resume_cmd: list[str] = field(default_factory=list)
    # Adapter-specific keys land in `extra`; each adapter pulls what it needs.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    refresh_interval: float = 2.0
    ai_title_interval: float = 3.0
    active_mtime_window_sec: int = 90
    ai_command: list[str] = field(default_factory=list)
    ai_timeout: int = 45
    focus_command: list[str] = field(default_factory=list)
    harnesses: list[HarnessConfig] = field(default_factory=list)
    # Raw dict, for debugging
    raw: dict[str, Any] = field(default_factory=dict)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _parse(raw: dict[str, Any]) -> AppConfig:
    app = raw.get("app", {})
    ai = raw.get("ai", {})
    focus = raw.get("focus", {})
    harnesses_raw = raw.get("harnesses", {})

    harnesses = []
    for name, cfg in harnesses_raw.items():
        if not isinstance(cfg, dict):
            continue
        # Pull known keys; stash everything else in `extra` for the adapter.
        known = {"kind", "enabled", "label", "color", "resume_cmd"}
        extra = {k: v for k, v in cfg.items() if k not in known}
        harnesses.append(HarnessConfig(
            name=name,
            kind=cfg.get("kind", name),
            enabled=bool(cfg.get("enabled", True)),
            label=str(cfg.get("label", name[0].upper() if name else "?")),
            color=str(cfg.get("color", "white")),
            resume_cmd=list(cfg.get("resume_cmd") or []),
            extra=extra,
        ))

    return AppConfig(
        refresh_interval=float(app.get("refresh_interval", 2.0)),
        ai_title_interval=float(app.get("ai_title_interval", 3.0)),
        active_mtime_window_sec=int(app.get("active_mtime_window_sec", 90)),
        ai_command=list(ai.get("command") or []),
        ai_timeout=int(ai.get("timeout", 45)),
        focus_command=list(focus.get("command") or []),
        harnesses=harnesses,
        raw=raw,
    )


def load(path: str = CONFIG_PATH) -> AppConfig:
    """Load config from ~/.config/htv/config.toml, merging with defaults.
    User config wins on conflicts. Missing file → defaults only."""
    raw = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                user_raw = tomllib.load(f)
            raw = _deep_merge(raw, user_raw)
        except Exception as e:
            # Corrupt config → fall through to defaults, but keep the error visible.
            raw["_load_error"] = f"{type(e).__name__}: {e}"
    return _parse(raw)


def write_default_if_missing(path: str = CONFIG_PATH) -> bool:
    """Write config.example.toml contents to `path` if it doesn't exist.
    Returns True if a file was written."""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example = os.path.join(pkg_dir, "config.example.toml")
    if os.path.exists(example):
        with open(example) as src, open(path, "w") as dst:
            dst.write(src.read())
        return True
    return False


__all__ = ["AppConfig", "HarnessConfig", "DEFAULT_CONFIG", "CONFIG_PATH", "load", "write_default_if_missing"]
