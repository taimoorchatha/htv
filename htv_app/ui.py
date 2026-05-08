"""Curses TUI — tabs, session list, smart-Enter, tail-view.

Design notes:
  * All adapter/config access runs through a single `State` object so the
    event loop stays short and testable.
  * `run_tui(cfg)` returns an *action tuple* which the outer CLI interprets
    AFTER curses has torn down. That lets us `os.execvp` a harness CLI
    without leaking curses state into the child terminal.
  * Step 3 ships: tabs · list · smart Enter · tail-view · refresh · quit.
    Sidecar name/tags, tag filter, tmux focus, AI features come in later steps.
"""
from __future__ import annotations

import curses
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .adapters import get_adapter_cls, Adapter
from .config import AppConfig
from .proc import ProcIndex
from .session import SessionRow


# ---- Tabs ----------------------------------------------------------------

TAB_ALL = "all"


@dataclass
class State:
    cfg: AppConfig
    adapters: dict[str, Adapter] = field(default_factory=dict)   # name → adapter
    rows_all: list[SessionRow] = field(default_factory=list)      # unfiltered, every harness
    rows: list[SessionRow] = field(default_factory=list)          # after filters
    procs: ProcIndex = field(default_factory=ProcIndex)
    sel: int = 0
    msg: str = ""                                                 # transient status line text

    # Filters
    tab: str = TAB_ALL          # "all" or an adapter name
    show_active: bool = True    # include active sessions too (unlike kirotv's default)

    last_refresh: float = 0.0


# ---- Setup ---------------------------------------------------------------

_COLOR_NAMES = {
    "black": curses.COLOR_BLACK, "red": curses.COLOR_RED, "green": curses.COLOR_GREEN,
    "yellow": curses.COLOR_YELLOW, "blue": curses.COLOR_BLUE, "magenta": curses.COLOR_MAGENTA,
    "cyan": curses.COLOR_CYAN, "white": curses.COLOR_WHITE,
}


def _init_colors(cfg: AppConfig) -> dict[str, int]:
    """Allocate one color pair per configured harness + a fallback. Returns name→pair_id."""
    curses.start_color()
    curses.use_default_colors()
    pair_map: dict[str, int] = {}
    # reserve pair 1 = selected row, pair 2 = status/footer
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    pair_map["_selected"] = 1
    pair_map["_footer"] = 2
    idx = 3
    for h in cfg.harnesses:
        color = _COLOR_NAMES.get(h.color.lower(), curses.COLOR_WHITE)
        curses.init_pair(idx, color, -1)
        pair_map[h.name] = idx
        idx += 1
    return pair_map


def _instantiate_adapters(cfg: AppConfig) -> dict[str, Adapter]:
    out: dict[str, Adapter] = {}
    for h in cfg.harnesses:
        if not h.enabled:
            continue
        cls = get_adapter_cls(h.kind)
        if cls is None:
            continue
        try:
            out[h.name] = cls(h)
        except Exception:
            continue
    return out


# ---- Data refresh --------------------------------------------------------

def _refresh(state: State) -> None:
    """Rescan /proc and re-run adapter.list_sessions()."""
    state.procs = ProcIndex()
    rows: list[SessionRow] = []
    for adapter in state.adapters.values():
        try:
            rows.extend(adapter.list_sessions(state.procs))
        except Exception as e:
            state.msg = f"· {adapter.name} error: {type(e).__name__}"
    # Global sort: active first, then most-recently-updated
    rows.sort(key=lambda r: r.updated, reverse=True)
    rows.sort(key=lambda r: 0 if r.active else 1)
    state.rows_all = rows
    _apply_filters(state)
    state.last_refresh = time.time()


def _apply_filters(state: State) -> None:
    out = state.rows_all
    if state.tab != TAB_ALL:
        out = [r for r in out if r.harness == state.tab]
    if not state.show_active:
        out = [r for r in out if not r.active]
    state.rows = out
    state.sel = min(state.sel, max(0, len(state.rows) - 1))


# ---- Formatting ----------------------------------------------------------

def _human_age(iso: str) -> str:
    if not iso:
        return "?"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        s = (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return "?"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s / 60)}m"
    if s < 86400:
        return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


def _shorten_cwd(cwd: str, home: str) -> str:
    if not cwd:
        return "?"
    if home and cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


# ---- Drawing -------------------------------------------------------------

def _draw(stdscr, state: State, pairs: dict[str, int]) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    home = os.path.expanduser("~")

    # --- Tab bar ---
    tabs: list[tuple[str, int, str]] = [(TAB_ALL, len(state.rows_all), "All")]
    for hc in state.cfg.harnesses:
        if not hc.enabled:
            continue
        count = sum(1 for r in state.rows_all if r.harness == hc.name)
        tabs.append((hc.name, count, hc.name.capitalize()))

    x = 1
    stdscr.addnstr(0, 0, " " * (w - 1), w - 1, curses.A_REVERSE)
    for name, count, label in tabs:
        text = f" {label} ({count}) "
        attr = curses.A_REVERSE
        if name == state.tab:
            attr = curses.A_BOLD | curses.A_UNDERLINE
        try:
            stdscr.addnstr(0, x, text, max(0, w - 1 - x), attr)
        except curses.error:
            pass
        x += len(text)

    # --- Column header ---
    header = f" {'ST':<2} {'H':<2} {'AGE':<5} {'MSG':>5}  {'CWD':<36} TITLE"
    stdscr.addnstr(1, 0, header.ljust(w - 1), w - 1, curses.A_BOLD)

    # --- Body ---
    body_h = h - 4  # 0=tabs, 1=header, last two = status + footer
    start = max(0, state.sel - body_h // 2)
    end = min(len(state.rows), start + body_h)

    # Active glyph pulses between "●" and "○" on a ~1s half-period.
    pulse_on = (int(time.time() * 1.4) % 2) == 0
    active_glyph = "●" if pulse_on else "○"

    for i, r in enumerate(state.rows[start:end]):
        y = 2 + i
        idx = start + i
        selected = (idx == state.sel)

        st = active_glyph if r.active else "·"
        age = _human_age(r.updated)
        cwd_s = _shorten_cwd(r.cwd, home)
        # Truncate cwd column so the title still gets real estate.
        cwd_w = max(10, min(40, w // 3))
        if len(cwd_s) > cwd_w:
            cwd_s = "…" + cwd_s[-(cwd_w - 1):]
        title = r.display_title
        # Harness label letter
        hcfg = next((h for h in state.cfg.harnesses if h.name == r.harness), None)
        letter = hcfg.label if hcfg else "?"

        # Build line piece-by-piece so the letter gets its own color pair.
        line_prefix = f" {st:<2} "
        letter_slot = f"{letter:<2}"
        line_rest = f" {age:<5} {r.msgs:>5}  {cwd_s:<{cwd_w}} {title}"

        row_attr = curses.A_NORMAL
        if selected:
            row_attr |= curses.color_pair(pairs["_selected"])
        if r.active:
            row_attr |= curses.A_BOLD

        try:
            stdscr.addnstr(y, 0, line_prefix, w - 1, row_attr)
            stdscr.addnstr(y, len(line_prefix), letter_slot, w - 1 - len(line_prefix),
                           row_attr | curses.color_pair(pairs.get(r.harness, 0)))
            rest_x = len(line_prefix) + len(letter_slot)
            stdscr.addnstr(y, rest_x, line_rest.ljust(w - 1 - rest_x), w - 1 - rest_x, row_attr)
        except curses.error:
            pass

    # --- Status line ---
    status_y = h - 2
    status = state.msg or f"{len(state.rows)} shown · {sum(1 for r in state.rows if r.active)} active"
    stdscr.addnstr(status_y, 0, status.ljust(w - 1), w - 1, curses.color_pair(pairs["_footer"]))

    # --- Footer ---
    foot = " ↑↓ nav · ⏎ resume · v view · a active · 1-4 tabs · r refresh · K kill · q quit "
    stdscr.addnstr(h - 1, 0, foot.ljust(w - 1), w - 1, curses.A_REVERSE)

    stdscr.refresh()


# ---- Tail view -----------------------------------------------------------

def _tail_view(stdscr, state: State, pairs: dict[str, int], row: SessionRow) -> None:
    """Read-only live-tail viewer for the selected session."""
    adapter = state.adapters.get(row.harness)
    if adapter is None:
        return

    last_mtime = -1.0
    follow = True
    scroll = 0
    rendered: list[tuple[int, str]] = []  # (attr, text)

    stdscr.nodelay(True)

    def rebuild() -> None:
        nonlocal rendered
        try:
            entries = adapter.tail_entries(row, n=10000)
        except Exception as e:
            rendered = [(curses.A_BOLD, f" error: {type(e).__name__}: {e}")]
            return
        h, w = stdscr.getmaxyx()
        wrap_w = max(20, w - 10)
        out: list[tuple[int, str]] = []
        for kind, preview in entries:
            attr = curses.A_NORMAL
            if kind == "USER":
                attr = curses.A_BOLD | curses.color_pair(pairs.get(row.harness, 0))
            elif kind == "TOOL":
                attr = curses.A_DIM
            label = f"[{kind}]"
            text = preview or "(empty)"
            first = True
            while text:
                chunk = text[:wrap_w]
                text = text[wrap_w:]
                prefix = f" {label:<6} " if first else f" {'':<6} "
                out.append((attr, prefix + chunk))
                first = False
        rendered = out

    while True:
        try:
            mtime = os.path.getmtime(row.jsonl) if os.path.exists(row.jsonl) else 0.0
        except OSError:
            mtime = 0.0
        if mtime != last_mtime:
            last_mtime = mtime
            rebuild()
            if follow:
                h, _w = stdscr.getmaxyx()
                scroll = max(0, len(rendered) - (h - 3))

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        head = f" {row.display_title[:w - 40]}  [{row.sid[:8]}]  {row.harness}  {'ACTIVE' if row.active else 'idle'} "
        stdscr.addnstr(0, 0, head.ljust(w - 1), w - 1, curses.A_REVERSE)
        body_h = h - 2
        end = min(len(rendered), scroll + body_h)
        for i, (attr, line) in enumerate(rendered[scroll:end]):
            try:
                stdscr.addnstr(1 + i, 0, line[:w - 1], w - 1, attr)
            except curses.error:
                pass
        foll = "FOLLOW" if follow else f"{scroll + 1}-{end}/{len(rendered)}"
        foot = f" ↑↓ scroll · g/G top/bot · f follow[{foll}] · q back "
        stdscr.addnstr(h - 1, 0, foot.ljust(w - 1), w - 1, curses.A_REVERSE)
        stdscr.refresh()

        c = stdscr.getch()
        if c == -1:
            time.sleep(0.2)
            continue
        if c in (ord('q'), 27):
            stdscr.nodelay(False)
            return
        if c in (curses.KEY_UP, ord('k')):
            scroll = max(0, scroll - 1); follow = False
        elif c in (curses.KEY_DOWN, ord('j')):
            scroll += 1
            if scroll >= max(0, len(rendered) - (h - 3)):
                scroll = max(0, len(rendered) - (h - 3))
                follow = True
        elif c == curses.KEY_PPAGE:
            scroll = max(0, scroll - (h - 3)); follow = False
        elif c == curses.KEY_NPAGE:
            scroll = min(max(0, len(rendered) - (h - 3)), scroll + (h - 3))
        elif c == ord('g'):
            scroll = 0; follow = False
        elif c == ord('G'):
            scroll = max(0, len(rendered) - (h - 3)); follow = True
        elif c == ord('f'):
            follow = not follow
            if follow:
                scroll = max(0, len(rendered) - (h - 3))


# ---- Event loop ----------------------------------------------------------

def _confirm(stdscr, prompt: str) -> bool:
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(h - 2, 0, f" {prompt} (y/N) ".ljust(w - 1), w - 1, curses.A_REVERSE | curses.A_BOLD)
    stdscr.refresh()
    stdscr.nodelay(False)
    c = stdscr.getch()
    stdscr.nodelay(True)
    return c in (ord('y'), ord('Y'))


def _tui(stdscr, cfg: AppConfig) -> Optional[tuple[str, Any]]:
    curses.curs_set(0)
    pairs = _init_colors(cfg)
    stdscr.nodelay(True)

    state = State(cfg=cfg)
    state.adapters = _instantiate_adapters(cfg)
    _refresh(state)

    tab_keys = {ord(str(i + 1)): t for i, t in enumerate([TAB_ALL] + [h.name for h in cfg.harnesses if h.enabled])}
    tab_order = [TAB_ALL] + [h.name for h in cfg.harnesses if h.enabled]

    while True:
        if time.time() - state.last_refresh > cfg.refresh_interval:
            _refresh(state)

        _draw(stdscr, state, pairs)
        state.msg = ""  # one-shot

        c = stdscr.getch()
        if c == -1:
            time.sleep(0.1)
            continue

        if c == ord('q'):
            return None

        elif c in (curses.KEY_UP, ord('k')):
            state.sel = max(0, state.sel - 1)
        elif c in (curses.KEY_DOWN, ord('j')):
            state.sel = min(len(state.rows) - 1, state.sel + 1)

        elif c in tab_keys:
            state.tab = tab_keys[c]
            _apply_filters(state)
            state.sel = 0
        elif c == 9:  # Tab
            cur = tab_order.index(state.tab) if state.tab in tab_order else 0
            state.tab = tab_order[(cur + 1) % len(tab_order)]
            _apply_filters(state); state.sel = 0
        elif c == 353:  # Shift-Tab (KEY_BTAB on many terms)
            cur = tab_order.index(state.tab) if state.tab in tab_order else 0
            state.tab = tab_order[(cur - 1) % len(tab_order)]
            _apply_filters(state); state.sel = 0

        elif c == ord('a'):
            state.show_active = not state.show_active
            _apply_filters(state)
            state.msg = f"· show_active = {state.show_active}"

        elif c == ord('r'):
            _refresh(state)
            state.msg = "· refreshed"

        elif c == ord('v'):
            if state.rows:
                _tail_view(stdscr, state, pairs, state.rows[state.sel])
                stdscr.nodelay(True)

        elif c in (curses.KEY_ENTER, 10, 13):
            if not state.rows:
                continue
            row = state.rows[state.sel]
            if row.active:
                state.msg = f"· active in pid {row.pid} — press K to kill first, or v to view"
                continue
            adapter = state.adapters.get(row.harness)
            if adapter is None:
                state.msg = "· no adapter"
                continue
            argv = adapter.resume_argv(row)
            if not argv:
                state.msg = "· no resume_cmd configured"
                continue
            return ("resume", {"cwd": row.cwd, "argv": argv, "sid": row.sid, "harness": row.harness})

        elif c == ord('K'):
            if not state.rows:
                continue
            row = state.rows[state.sel]
            if not row.active or not row.pid:
                state.msg = "· not active"
                continue
            if _confirm(stdscr, f"Kill PID {row.pid} ({row.display_title[:40]})?"):
                try:
                    os.kill(row.pid, 15)
                    state.msg = f"· sent SIGTERM to {row.pid}"
                    time.sleep(0.3)
                    _refresh(state)
                except Exception as e:
                    state.msg = f"· kill failed: {e}"


def run_tui(cfg: AppConfig) -> Optional[tuple[str, Any]]:
    """Entry point. Returns an action tuple (e.g. ('resume', {...})) or None."""
    return curses.wrapper(_tui, cfg)


__all__ = ["run_tui"]
