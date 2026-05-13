"""Entry point: `python3 -m htv_app` — launches TUI; `htv doctor` for a smoke test."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from . import adapters as _adapters_pkg  # noqa: F401 — registers built-ins
from .adapters import all_kinds, get_adapter_cls
from .config import CONFIG_PATH, load, write_default_if_missing
from .proc import ProcIndex


def cmd_doctor(args: argparse.Namespace) -> int:
    """Smoke test: load config, build proc index, run adapters, print sample rows."""
    t0 = time.time()
    wrote = write_default_if_missing()
    cfg = load()

    print(f"htv {__version__}")
    print(f"config:    {CONFIG_PATH}" + ("  (wrote default)" if wrote else ""))
    if "_load_error" in cfg.raw:
        print(f"  ⚠ load error: {cfg.raw['_load_error']}")
    print(f"refresh:   {cfg.refresh_interval}s")
    print(f"ai_cmd:    {cfg.ai_command or '(disabled)'}")
    print(f"focus:     {cfg.focus_command or '(disabled)'}")
    print(f"adapters registered: {', '.join(all_kinds()) or '(none)'}")
    print()
    print("harnesses:")
    for h in cfg.harnesses:
        cls = get_adapter_cls(h.kind)
        marker = "✓" if (cls and h.enabled) else ("✗ disabled" if not h.enabled else f"✗ no adapter for kind={h.kind!r}")
        print(f"  [{h.label}] {h.name:<12} kind={h.kind:<8} {marker}")
        if h.extra:
            for k, v in h.extra.items():
                pv = os.path.expanduser(v) if isinstance(v, str) else v
                exists = ""
                if isinstance(pv, str):
                    exists = "  (exists)" if os.path.isdir(pv) else "  (missing)"
                print(f"       {k} = {pv}{exists}")

    print()
    print("scanning processes…")
    procs = ProcIndex()
    print(f"  {procs}")
    harness_procs = [p for p in procs.all() if p.comm in ("kiro-cli", "claude", "pi")]
    for p in harness_procs[:10]:
        print(f"    pid={p.pid:<7} comm={p.comm:<10} cwd={p.cwd}")
    if len(harness_procs) > 10:
        print(f"    ... and {len(harness_procs) - 10} more")

    print()
    print("instantiating adapters…")
    home = os.path.expanduser("~")
    for h in cfg.harnesses:
        if not h.enabled:
            continue
        cls = get_adapter_cls(h.kind)
        if cls is None:
            continue
        try:
            adapter = cls(h)
            rows = adapter.list_sessions(procs)
            active = sum(1 for r in rows if r.active)
            print(f"  {h.name:<12} ({h.kind:<8}) → {len(rows):>3} sessions  ({active} active)")
            rows_sorted = sorted(rows, key=lambda r: r.updated, reverse=True)
            rows_sorted.sort(key=lambda r: 0 if r.active else 1)
            for r in rows_sorted[:3]:
                badge = "●" if r.active else "·"
                short_cwd = r.cwd.replace(home, "~") if r.cwd else "?"
                title = r.display_title
                if len(title) > 58:
                    title = title[:57] + "…"
                print(f"      {badge} {r.sid[:8]}  {r.msgs:>4} msgs  {short_cwd}")
                print(f"        ↳ {title}")
        except Exception as e:
            print(f"  {h.name:<12} ({h.kind:<8}) → ERROR: {type(e).__name__}: {e}")
            if getattr(args, "verbose", False):
                import traceback
                traceback.print_exc()

    print(f"\ndone in {(time.time() - t0) * 1000:.0f}ms")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """Launch the curses dashboard. On 'resume' action, chdir+execvp the harness CLI."""
    write_default_if_missing()
    cfg = load()
    from .ui import run_tui
    try:
        action = run_tui(cfg)
    except Exception as e:
        import _curses
        if isinstance(e, _curses.error):
            print(f"htv: terminal init failed ({e}).", file=sys.stderr)
            print("     Needs an interactive, color-capable TTY. Try kitty / alacritty / iTerm2 /", file=sys.stderr)
            print("     gnome-terminal. `htv doctor` runs without curses if you need a sanity check.", file=sys.stderr)
            return 1
        raise
    if action is None:
        return 0
    kind, payload = action
    if kind == "resume":
        cwd = payload.get("cwd") or os.getcwd()
        argv = payload.get("argv") or []
        if not argv:
            print("no resume command", file=sys.stderr)
            return 2
        try:
            if os.path.isdir(cwd):
                os.chdir(cwd)
        except OSError as e:
            print(f"chdir {cwd!r} failed: {e}", file=sys.stderr)
        try:
            os.execvp(argv[0], argv)
        except FileNotFoundError:
            print(f"not found: {argv[0]!r}", file=sys.stderr)
            return 127

    if kind == "tmux-attach":
        target = payload.get("target")
        if not target:
            return 2
        # Inside tmux: switch-client to the pane/session. Outside: attach.
        tmux_cmd = ["tmux", "switch-client", "-t", target] if os.environ.get("TMUX") \
                   else ["tmux", "attach-session", "-t", target.split(":", 1)[0]]
        try:
            os.execvp(tmux_cmd[0], tmux_cmd)
        except FileNotFoundError:
            print("tmux not installed", file=sys.stderr)
            return 127

    return 0


def _check_python_version() -> None:
    """Abort with a friendly error if Python < 3.11 (tomllib is stdlib since 3.11)."""
    if sys.version_info < (3, 11):
        v = ".".join(str(x) for x in sys.version_info[:3])
        print(f"htv requires Python 3.11+ (detected {v}). It uses stdlib tomllib.", file=sys.stderr)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    _check_python_version()
    p = argparse.ArgumentParser(prog="htv", description="Harness session dashboard — kiro / claude / pi")
    p.add_argument("--version", action="version", version=f"htv {__version__}")
    sub = p.add_subparsers(dest="cmd")

    p_doctor = sub.add_parser("doctor", help="Print config + adapter status, then exit")
    p_doctor.add_argument("-v", "--verbose", action="store_true", help="Show tracebacks on adapter errors")
    p_doctor.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    if getattr(args, "cmd", None) is None:
        return cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
