"""Curses TUI — tabs, session list, smart-Enter, tail-view.

Design notes:
  * All UI state lives on a single `State` object so the event loop stays short.
  * `run_tui(cfg)` returns an *action tuple* which the outer CLI interprets
    AFTER curses has torn down (e.g. ``("resume", {...})``). That lets us
    `os.execvp` a harness CLI without leaking curses state into the child.
  * Each draw-/key-handler is kept under ~40 LOC so the whole thing fits on a
    single screen. See `bin/review` for the enforcement.
"""
from __future__ import annotations

import curses
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .adapters import Adapter, get_adapter_cls
from .config import AppConfig
from .proc import ProcIndex
from .session import SessionRow
from . import sidecar


TAB_ALL = "all"


# ---- State ---------------------------------------------------------------

@dataclass
class State:
    cfg: AppConfig
    adapters: dict[str, Adapter] = field(default_factory=dict)
    rows_all: list[SessionRow] = field(default_factory=list)
    rows: list[SessionRow] = field(default_factory=list)          # post-filter
    procs: ProcIndex = field(default_factory=ProcIndex)
    sel: int = 0
    msg: str = ""                                                 # transient status
    tab: str = TAB_ALL
    show_active: bool = True
    last_refresh: float = 0.0


# ---- Setup ---------------------------------------------------------------

_COLOR_NAMES = {
    "black": curses.COLOR_BLACK, "red": curses.COLOR_RED, "green": curses.COLOR_GREEN,
    "yellow": curses.COLOR_YELLOW, "blue": curses.COLOR_BLUE, "magenta": curses.COLOR_MAGENTA,
    "cyan": curses.COLOR_CYAN, "white": curses.COLOR_WHITE,
}


def _init_colors(cfg: AppConfig) -> dict[str, int]:
    """Allocate one color pair per harness plus two reserved (selected, footer)."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)   # selected row
    curses.init_pair(2, curses.COLOR_YELLOW, -1)                 # footer text
    pair_map = {"_selected": 1, "_footer": 2}
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


# ---- Refresh & filter ----------------------------------------------------

def _refresh(state: State) -> None:
    state.procs = ProcIndex()
    rows: list[SessionRow] = []
    for adapter in state.adapters.values():
        try:
            rows.extend(adapter.list_sessions(state.procs))
        except Exception as e:
            state.msg = f"· {adapter.name} error: {type(e).__name__}"
    # Attach user-assigned sidecar name/tags (best-effort, missing sidecars → empty).
    for r in rows:
        meta = sidecar.load(r.jsonl)
        r.name = meta.get("name", "")
        r.tags = meta.get("tags", [])
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
    if s < 60: return f"{int(s)}s"
    if s < 3600: return f"{int(s / 60)}m"
    if s < 86400: return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


def _shorten_cwd(cwd: str, home: str) -> str:
    if not cwd:
        return "?"
    if home and cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def _pulse_glyph(t: float) -> str:
    """Pulse ● ↔ ○ at ~0.7s half-period."""
    return "●" if (int(t * 1.4) % 2) == 0 else "○"


# ---- Drawing -------------------------------------------------------------

def _draw_tab_bar(stdscr, state: State, w: int) -> None:
    """Render the tab row at y=0."""
    tabs: list[tuple[str, int, str]] = [(TAB_ALL, len(state.rows_all), "All")]
    for hc in state.cfg.harnesses:
        if not hc.enabled:
            continue
        count = sum(1 for r in state.rows_all if r.harness == hc.name)
        tabs.append((hc.name, count, hc.name.capitalize()))
    stdscr.addnstr(0, 0, " " * (w - 1), w - 1, curses.A_REVERSE)
    x = 1
    for name, count, label in tabs:
        text = f" {label} ({count}) "
        attr = curses.A_BOLD | curses.A_UNDERLINE if name == state.tab else curses.A_REVERSE
        try:
            stdscr.addnstr(0, x, text, max(0, w - 1 - x), attr)
        except curses.error:
            pass
        x += len(text)


def _draw_row(stdscr, y: int, r: SessionRow, selected: bool,
              state: State, pairs: dict[str, int], w: int, home: str, glyph: str) -> None:
    """Render one session row at `y`."""
    st = glyph if r.active else "·"
    age = _human_age(r.updated)
    cwd_w = max(10, min(40, w // 3))
    cwd_s = _shorten_cwd(r.cwd, home)
    if len(cwd_s) > cwd_w:
        cwd_s = "…" + cwd_s[-(cwd_w - 1):]
    hcfg = next((h for h in state.cfg.harnesses if h.name == r.harness), None)
    letter = hcfg.label if hcfg else "?"

    prefix = f" {st:<2} "
    letter_slot = f"{letter:<2}"
    rest = f" {age:<5} {r.msgs:>5}  {cwd_s:<{cwd_w}} {r.display_title}"

    row_attr = curses.A_NORMAL
    if selected:
        row_attr |= curses.color_pair(pairs["_selected"])
    if r.active:
        row_attr |= curses.A_BOLD

    try:
        stdscr.addnstr(y, 0, prefix, w - 1, row_attr)
        stdscr.addnstr(y, len(prefix), letter_slot, w - 1 - len(prefix),
                       row_attr | curses.color_pair(pairs.get(r.harness, 0)))
        rest_x = len(prefix) + len(letter_slot)
        stdscr.addnstr(y, rest_x, rest.ljust(w - 1 - rest_x), w - 1 - rest_x, row_attr)
    except curses.error:
        pass


def _draw_footer(stdscr, state: State, pairs: dict[str, int], h: int, w: int) -> None:
    """Two-line footer: status + keybindings."""
    status = state.msg or f"{len(state.rows)} shown · {sum(1 for r in state.rows if r.active)} active"
    stdscr.addnstr(h - 2, 0, status.ljust(w - 1), w - 1, curses.color_pair(pairs["_footer"]))
    foot = " ↑↓ nav · ⏎ resume · v view · r rename · a active · 1-4 tabs · K kill · q quit "
    stdscr.addnstr(h - 1, 0, foot.ljust(w - 1), w - 1, curses.A_REVERSE)


def _draw(stdscr, state: State, pairs: dict[str, int]) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    home = os.path.expanduser("~")
    _draw_tab_bar(stdscr, state, w)
    header = f" {'ST':<2} {'H':<2} {'AGE':<5} {'MSG':>5}  {'CWD':<36} TITLE"
    stdscr.addnstr(1, 0, header.ljust(w - 1), w - 1, curses.A_BOLD)
    body_h = h - 4
    start = max(0, state.sel - body_h // 2)
    end = min(len(state.rows), start + body_h)
    glyph = _pulse_glyph(time.time())
    for i, r in enumerate(state.rows[start:end]):
        _draw_row(stdscr, 2 + i, r, (start + i) == state.sel, state, pairs, w, home, glyph)
    _draw_footer(stdscr, state, pairs, h, w)
    stdscr.refresh()


# ---- Tail view -----------------------------------------------------------

def _tail_render(adapter: Adapter, row: SessionRow, pairs: dict[str, int],
                 wrap_w: int) -> list[tuple[int, str]]:
    """Build the wrapped (attr, line) list for the tail-view buffer."""
    try:
        entries = adapter.tail_entries(row, n=10000)
    except Exception as e:
        return [(curses.A_BOLD, f" error: {type(e).__name__}: {e}")]
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
            chunk, text = text[:wrap_w], text[wrap_w:]
            prefix = f" {label:<6} " if first else f" {'':<6} "
            out.append((attr, prefix + chunk))
            first = False
    return out


def _tail_handle_key(c: int, scroll: int, follow: bool,
                     rendered_len: int, body_h: int) -> tuple[int, bool, bool]:
    """Return (new_scroll, new_follow, should_quit)."""
    if c in (ord('q'), 27):
        return scroll, follow, True
    max_scroll = max(0, rendered_len - body_h)
    if c in (curses.KEY_UP, ord('k')):
        return max(0, scroll - 1), False, False
    if c in (curses.KEY_DOWN, ord('j')):
        new = min(max_scroll, scroll + 1)
        return new, (new >= max_scroll), False
    if c == curses.KEY_PPAGE:
        return max(0, scroll - body_h), False, False
    if c == curses.KEY_NPAGE:
        return min(max_scroll, scroll + body_h), False, False
    if c == ord('g'):
        return 0, False, False
    if c == ord('G'):
        return max_scroll, True, False
    if c == ord('f'):
        f = not follow
        return (max_scroll if f else scroll), f, False
    return scroll, follow, False


def _tail_view(stdscr, state: State, pairs: dict[str, int], row: SessionRow) -> None:
    adapter = state.adapters.get(row.harness)
    if adapter is None:
        return
    stdscr.nodelay(True)
    last_mtime = -1.0
    follow = True
    scroll = 0
    rendered: list[tuple[int, str]] = []

    while True:
        try:
            mtime = os.path.getmtime(row.jsonl) if os.path.exists(row.jsonl) else 0.0
        except OSError:
            mtime = 0.0
        h, w = stdscr.getmaxyx()
        body_h = h - 2
        if mtime != last_mtime:
            last_mtime = mtime
            rendered = _tail_render(adapter, row, pairs, max(20, w - 10))
            if follow:
                scroll = max(0, len(rendered) - body_h)

        stdscr.erase()
        head = f" {row.display_title[:w - 40]}  [{row.sid[:8]}]  {row.harness}  {'ACTIVE' if row.active else 'idle'} "
        stdscr.addnstr(0, 0, head.ljust(w - 1), w - 1, curses.A_REVERSE)
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
        scroll, follow, quit_ = _tail_handle_key(c, scroll, follow, len(rendered), body_h)
        if quit_:
            stdscr.nodelay(False)
            return


# ---- Event loop (list view) ---------------------------------------------

def _confirm(stdscr, prompt: str) -> bool:
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(h - 2, 0, f" {prompt} (y/N) ".ljust(w - 1), w - 1, curses.A_REVERSE | curses.A_BOLD)
    stdscr.refresh()
    stdscr.nodelay(False)
    c = stdscr.getch()
    stdscr.nodelay(True)
    return c in (ord('y'), ord('Y'))


def _prompt_text(stdscr, prompt: str, default: str = "") -> Optional[str]:
    """Bottom-line text input. Returns the string on Enter, None on Esc."""
    h, w = stdscr.getmaxyx()
    curses.curs_set(1)
    stdscr.nodelay(False)
    buf = list(default)
    pos = len(buf)
    try:
        while True:
            line = f" {prompt}: {''.join(buf)}"
            stdscr.addnstr(h - 1, 0, line.ljust(w - 1), w - 1, curses.A_REVERSE | curses.A_BOLD)
            stdscr.move(h - 1, min(len(f" {prompt}: ") + pos, w - 2))
            stdscr.refresh()
            c = stdscr.getch()
            if c in (10, 13, curses.KEY_ENTER):
                return "".join(buf).strip()
            if c == 27:
                return None
            if c in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    buf.pop(pos - 1); pos -= 1
            elif c == curses.KEY_LEFT and pos > 0:
                pos -= 1
            elif c == curses.KEY_RIGHT and pos < len(buf):
                pos += 1
            elif 32 <= c < 127:
                buf.insert(pos, chr(c)); pos += 1
    finally:
        curses.curs_set(0)
        stdscr.nodelay(True)


def _handle_rename(stdscr, state: State) -> None:
    if not state.rows:
        return
    row = state.rows[state.sel]
    new_name = _prompt_text(stdscr, "name", default=row.name)
    if new_name is None:
        state.msg = "· cancelled"
        return
    try:
        sidecar.save(row.jsonl, name=new_name, tags=row.tags)
    except OSError as e:
        state.msg = f"· save failed: {e}"
        return
    row.name = new_name
    state.msg = f"· renamed: {new_name or '(cleared)'}"


def _switch_tab(c: int, state: State, tab_order: list[str],
                tab_keys: dict[int, str]) -> bool:
    """Handle tab-switch keys. Returns True if the key was consumed."""
    if c in tab_keys:
        state.tab = tab_keys[c]
    elif c == 9:  # Tab
        cur = tab_order.index(state.tab) if state.tab in tab_order else 0
        state.tab = tab_order[(cur + 1) % len(tab_order)]
    elif c == 353:  # Shift-Tab (KEY_BTAB on many terms)
        cur = tab_order.index(state.tab) if state.tab in tab_order else 0
        state.tab = tab_order[(cur - 1) % len(tab_order)]
    else:
        return False
    _apply_filters(state)
    state.sel = 0
    return True


def _handle_enter(state: State) -> Optional[tuple[str, Any]]:
    """Enter key: return a resume action, or set state.msg and return None."""
    if not state.rows:
        return None
    row = state.rows[state.sel]
    if row.active:
        state.msg = f"· active in pid {row.pid} — press K to kill first, or v to view"
        return None
    adapter = state.adapters.get(row.harness)
    if adapter is None:
        state.msg = "· no adapter"
        return None
    argv = adapter.resume_argv(row)
    if not argv:
        state.msg = "· no resume_cmd configured"
        return None
    return ("resume", {"cwd": row.cwd, "argv": argv, "sid": row.sid, "harness": row.harness})


def _handle_kill(stdscr, state: State) -> None:
    if not state.rows:
        return
    row = state.rows[state.sel]
    if not row.active or not row.pid:
        state.msg = "· not active"
        return
    if not _confirm(stdscr, f"Kill PID {row.pid} ({row.display_title[:40]})?"):
        return
    try:
        os.kill(row.pid, 15)
        state.msg = f"· sent SIGTERM to {row.pid}"
        time.sleep(0.3)
        _refresh(state)
    except Exception as e:
        state.msg = f"· kill failed: {e}"


def _tui(stdscr, cfg: AppConfig) -> Optional[tuple[str, Any]]:
    curses.curs_set(0)
    pairs = _init_colors(cfg)
    stdscr.nodelay(True)

    state = State(cfg=cfg)
    state.adapters = _instantiate_adapters(cfg)
    _refresh(state)

    tab_order = [TAB_ALL] + [h.name for h in cfg.harnesses if h.enabled]
    tab_keys = {ord(str(i + 1)): t for i, t in enumerate(tab_order)}

    while True:
        if time.time() - state.last_refresh > cfg.refresh_interval:
            _refresh(state)
        _draw(stdscr, state, pairs)
        state.msg = ""

        c = stdscr.getch()
        if c == -1:
            time.sleep(0.1); continue
        if c == ord('q'):
            return None
        if c in (curses.KEY_UP, ord('k')):
            state.sel = max(0, state.sel - 1)
        elif c in (curses.KEY_DOWN, ord('j')):
            state.sel = min(len(state.rows) - 1, state.sel + 1)
        elif _switch_tab(c, state, tab_order, tab_keys):
            pass
        elif c == ord('a'):
            state.show_active = not state.show_active
            _apply_filters(state)
            state.msg = f"· show_active = {state.show_active}"
        elif c == ord('r'):
            _handle_rename(stdscr, state)
        elif c == ord('v') and state.rows:
            _tail_view(stdscr, state, pairs, state.rows[state.sel])
            stdscr.nodelay(True)
        elif c in (curses.KEY_ENTER, 10, 13):
            action = _handle_enter(state)
            if action is not None:
                return action
        elif c == ord('K'):
            _handle_kill(stdscr, state)


def run_tui(cfg: AppConfig) -> Optional[tuple[str, Any]]:
    """Entry point. Returns an action tuple (e.g. ('resume', {...})) or None."""
    return curses.wrapper(_tui, cfg)


__all__ = ["run_tui"]
