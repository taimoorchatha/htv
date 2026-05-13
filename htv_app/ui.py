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
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .adapters import Adapter, get_adapter_cls
from .config import AppConfig
from .proc import ProcIndex
from .session import SessionRow
from . import sidecar, tmux_util


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
    tag_filter: str = ""        # "" = no tag filter active
    search_query: str = ""      # "" = no fuzzy search (committed)
    search_mode: bool = False   # True when user is typing in the search box
    last_refresh: float = 0.0
    # Concurrency: the refresh worker rebuilds rows_all/procs on a background
    # thread; the UI thread reads them under `lock`. `wake` lets handlers ask
    # for an immediate refresh (e.g. after SIGTERM-ing a session). `stop` ends
    # the worker on clean exit.
    lock: threading.Lock = field(default_factory=threading.Lock)
    wake: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)


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
    """Rebuild rows_all + procs. Safe to call from any thread — the brief
    write of state under `state.lock` is the only point of contention with the
    UI thread (which only reads, also under the lock).
    """
    procs = ProcIndex()
    rows: list[SessionRow] = []
    err: Optional[str] = None
    for adapter in state.adapters.values():
        try:
            rows.extend(adapter.list_sessions(procs))
        except Exception as e:
            err = f"· {adapter.name} error: {type(e).__name__}"
    # Attach user-assigned sidecar name/tags (best-effort, missing sidecars → empty).
    for r in rows:
        meta = sidecar.load(r.jsonl)
        r.name = meta.get("name", "")
        r.tags = meta.get("tags", [])
    rows.sort(key=lambda r: r.updated, reverse=True)
    rows.sort(key=lambda r: 0 if r.active else 1)
    with state.lock:
        state.procs = procs
        state.rows_all = rows
        if err:
            state.msg = err
        state.last_refresh = time.time()
        _apply_filters(state)


def _refresh_worker(state: State) -> None:
    """Background loop: refresh on a fixed interval, or whenever a handler asks
    for an early refresh via `state.wake.set()`. Stops when `state.stop` fires.
    """
    while not state.stop.is_set():
        try:
            _refresh(state)
        except Exception as e:
            with state.lock:
                state.msg = f"· refresh error: {type(e).__name__}"
        # Sleep up to refresh_interval, but wake immediately on demand.
        state.wake.wait(timeout=state.cfg.refresh_interval)
        state.wake.clear()


def _subseq_match(needle: str, haystack: str) -> bool:
    """Fuzzy: are all chars of `needle` in `haystack` in order? Case-sensitive caller."""
    i = 0
    for c in haystack:
        if i < len(needle) and c == needle[i]:
            i += 1
    return i == len(needle)


def _match_positions(needle: str, haystack: str) -> set[int]:
    """Indices in `haystack` that correspond to `needle`'s match.
    Prefers contiguous substring; falls back to subsequence. Returns empty
    set when the haystack doesn't actually contain the needle."""
    if not needle:
        return set()
    n, h = needle.lower(), haystack.lower()
    idx = h.find(n)
    if idx >= 0:
        return set(range(idx, idx + len(n)))
    positions: set[int] = set()
    i = 0
    for j, c in enumerate(h):
        if i < len(n) and c == n[i]:
            positions.add(j)
            i += 1
    return positions if i == len(n) else set()


def _apply_filters(state: State) -> None:
    out = state.rows_all
    if state.tab != TAB_ALL:
        out = [r for r in out if r.harness == state.tab]
    if not state.show_active:
        out = [r for r in out if not r.active]
    if state.tag_filter:
        tag = state.tag_filter.lower()
        out = [r for r in out if any(t.lower() == tag for t in r.tags)]
    if state.search_query:
        q = state.search_query.lower()
        exact, subseq = [], []
        for r in out:
            hay = f"{r.name} {r.display_title} {r.cwd}".lower()
            if q in hay:
                exact.append(r)
            elif _subseq_match(q, hay):
                subseq.append(r)
        out = exact + subseq
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


def _marquee(text: str, width: int, now: float, speed: float = 4.0) -> str:
    """Scroll `text` leftward through a window of `width` chars at `speed` chars/sec.
    Seamless loop via a gap separator. If the text fits, return it padded."""
    if len(text) <= width:
        return text.ljust(width)
    loop = text + "   ·   "
    off = int(now * speed) % len(loop)
    return (loop * 2)[off:off + width]


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

    # Bold the characters that matched the active search so the user can see WHY.
    if state.search_query:
        # Column offsets: prefix + letter + " age>5 msgs>5  " then cwd<cwd_w then " " then title.
        static_mid = f" {age:<5} {r.msgs:>5}  "
        cwd_start = len(prefix) + len(letter_slot) + len(static_mid)
        title_start = cwd_start + cwd_w + 1
        _highlight(stdscr, y, cwd_start, cwd_s, state.search_query, row_attr)
        _highlight(stdscr, y, title_start, r.display_title, state.search_query, row_attr)


def _highlight(stdscr, y: int, x: int, text: str, query: str, base_attr: int) -> None:
    """Overwrite matched characters of `text` at (y, x+pos) with A_BOLD on top of base_attr."""
    for pos in _match_positions(query, text):
        if pos >= len(text):
            continue
        try:
            stdscr.addnstr(y, x + pos, text[pos], 1, base_attr | curses.A_BOLD)
        except curses.error:
            return


def _draw_footer(stdscr, state: State, pairs: dict[str, int], h: int, w: int) -> None:
    """Two-line footer. In search_mode: status summary + live / prompt with cursor.
    Normal: status (filter chip / selected-row tags) + marquee keybindings."""
    if state.search_mode:
        summary = f"{len(state.rows)} match(es) · Enter to keep · Esc to clear"
        stdscr.addnstr(h - 2, 0, summary.ljust(w - 1), w - 1, curses.color_pair(pairs["_footer"]))
        prompt = f" /{state.search_query}"
        stdscr.addnstr(h - 1, 0, prompt.ljust(w - 1), w - 1, curses.A_REVERSE | curses.A_BOLD)
        try:
            curses.curs_set(1)
            stdscr.move(h - 1, min(len(prompt), w - 1))
        except curses.error:
            pass
        return
    curses.curs_set(0)
    if state.msg:
        status = state.msg
    else:
        bits = [f"{len(state.rows)} shown", f"{sum(1 for r in state.rows if r.active)} active"]
        if state.search_query:
            bits.insert(0, f"search={state.search_query!r}")
        if state.tag_filter:
            bits.insert(0, f"tag={state.tag_filter}")
        if not state.search_query and not state.tag_filter and state.rows and state.rows[state.sel].tags:
            bits.append("tags: " + " ".join("#" + t for t in state.rows[state.sel].tags))
        status = " · ".join(bits)
    stdscr.addnstr(h - 2, 0, status.ljust(w - 1), w - 1, curses.color_pair(pairs["_footer"]))
    foot_text = " ↑↓ nav · ⏎ resume · t tmux · v view · / search · r rename · # tags · F filter · a active · 1-4 · K kill · q quit "
    foot = _marquee(foot_text, w - 1, time.time())
    stdscr.addnstr(h - 1, 0, foot, w - 1, curses.A_REVERSE)


def _draw_empty_state(stdscr, h: int, w: int) -> None:
    """Centered helper when there are no sessions anywhere (new user, no CLIs used yet)."""
    lines = [
        "No sessions found.",
        "",
        "Use one of these CLIs once to create a session:",
        "  kiro-cli chat   (creates ~/.kiro/sessions/cli/)",
        "  claude          (creates ~/.claude/projects/)",
        "  pi              (creates ~/.pi/agent/sessions/)",
        "",
        "Then run htv again. For diagnostics: htv doctor.",
    ]
    y0 = max(3, (h - len(lines)) // 2)
    for i, line in enumerate(lines):
        try:
            stdscr.addnstr(y0 + i, max(2, (w - len(line)) // 2), line, w - 2, curses.A_DIM)
        except curses.error:
            pass


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
    if not state.rows_all:
        _draw_empty_state(stdscr, h, w)
    else:
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


def _tail_dispatch_search(c: int, stdscr, sq: str, matches: list[int], match_idx: int,
                          scroll: int, follow: bool, body_h: int,
                          rendered: list[tuple[int, str]]) -> tuple:
    """Handle /, n, N, Esc in the tail view. Returns updated state + `consumed` flag.
    consumed=True means the key was handled; caller should skip the generic tail handler."""
    if c == ord('/'):
        new_q = _prompt_text(stdscr, "find in conversation", default=sq)
        if new_q is not None:
            sq = new_q.strip()
            matches = _find_matches(rendered, sq)
            match_idx = 0
            if matches:
                scroll = max(0, matches[0] - body_h // 2); follow = False
        return sq, matches, match_idx, scroll, follow, True
    if c == ord('n') and matches:
        match_idx = (match_idx + 1) % len(matches)
        scroll = max(0, matches[match_idx] - body_h // 2); follow = False
        return sq, matches, match_idx, scroll, follow, True
    if c == ord('N') and matches:
        match_idx = (match_idx - 1) % len(matches)
        scroll = max(0, matches[match_idx] - body_h // 2); follow = False
        return sq, matches, match_idx, scroll, follow, True
    if c == 27 and sq:  # Esc clears the active search before falling back to quit
        return "", [], 0, scroll, follow, True
    return sq, matches, match_idx, scroll, follow, False


def _find_matches(rendered: list[tuple[int, str]], query: str) -> list[int]:
    """Return line indices (in `rendered`) containing `query` (case-insensitive)."""
    if not query:
        return []
    q = query.lower()
    return [i for i, (_, line) in enumerate(rendered) if q in line.lower()]


def _tail_view(stdscr, state: State, pairs: dict[str, int], row: SessionRow) -> None:
    adapter = state.adapters.get(row.harness)
    if adapter is None:
        return
    stdscr.nodelay(True)
    last_mtime = -1.0
    follow = True
    scroll = 0
    rendered: list[tuple[int, str]] = []
    # In-conversation search state
    search_query = ""
    matches: list[int] = []
    match_idx = 0

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
            matches = _find_matches(rendered, search_query)
            match_idx = min(match_idx, len(matches) - 1) if matches else 0
            if follow:
                scroll = max(0, len(rendered) - body_h)

        stdscr.erase()
        head = f" {row.display_title[:w - 40]}  [{row.sid[:8]}]  {row.harness}  {'ACTIVE' if row.active else 'idle'} "
        stdscr.addnstr(0, 0, head.ljust(w - 1), w - 1, curses.A_REVERSE)
        match_set = set(matches)
        end = min(len(rendered), scroll + body_h)
        for i, (attr, line) in enumerate(rendered[scroll:end]):
            draw_attr = attr | (curses.A_STANDOUT if (scroll + i) in match_set else 0)
            try:
                stdscr.addnstr(1 + i, 0, line[:w - 1], w - 1, draw_attr)
            except curses.error:
                pass
        if search_query:
            foot = f" /{search_query!r} · n/N next/prev [{match_idx + 1 if matches else 0}/{len(matches)}] · Esc clear · q back "
        else:
            foll = "FOLLOW" if follow else f"{scroll + 1}-{end}/{len(rendered)}"
            foot = f" ↑↓ scroll · g/G top/bot · f follow[{foll}] · / search · q back "
        stdscr.addnstr(h - 1, 0, foot.ljust(w - 1), w - 1, curses.A_REVERSE)
        stdscr.refresh()

        c = stdscr.getch()
        if c == -1:
            time.sleep(0.2)
            continue
        # Try search keys first; they take precedence over generic tail keys.
        sq, matches, match_idx, scroll, follow, consumed = _tail_dispatch_search(
            c, stdscr, search_query, matches, match_idx, scroll, follow, body_h, rendered)
        search_query = sq
        if consumed:
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
    with state.lock:
        _apply_filters(state)
        state.sel = 0
    return True


def _gather_active_info(row: SessionRow) -> dict:
    """Pid / tty / tmux-pane / kitty-window info for the modal body."""
    return {
        "pid": row.pid,
        "tty": tmux_util.tty_of(row.pid or 0) or "?",
        "tmux_pane": tmux_util.find_tmux_pane(row.pid or 0) or "",
        "kitty_win": tmux_util.find_kitty_window(row.pid or 0) or "",
    }


def _draw_active_modal(stdscr, row: SessionRow, info: dict) -> None:
    """Render a centered modal explaining the active state + options."""
    h, w = stdscr.getmaxyx()
    body = [
        f"Session:   {row.display_title[:56]}",
        f"Harness:   {row.harness}",
        "",
        f"Running in pid {info['pid']}",
        f"tty:       {info['tty']}",
        f"in tmux:   {info['tmux_pane'] or 'no (bare tty)'}",
    ]
    if info["kitty_win"]:
        body.append(f"kitty win: {info['kitty_win']}")
    body += [
        "",
        "What do you want to do?",
        "  [t]    tmux attach / focus the window",
        f"  [K]    kill pid {info['pid']} (then Enter to resume)",
        "  [v]    view conversation tail (read-only)",
        "  [esc]  cancel",
    ]
    inner_w = max(56, max(len(line) for line in body) + 2)
    box_w = min(w - 4, inner_w + 2)
    box_h = len(body) + 4
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    stdscr.addnstr(y0, x0, "╭─ already running " + "─" * max(0, box_w - 20) + "╮", box_w, curses.A_REVERSE | curses.A_BOLD)
    for i, line in enumerate(body + [""]):
        stdscr.addnstr(y0 + 1 + i, x0, "│ " + line.ljust(box_w - 3) + "│", box_w, curses.A_REVERSE)
    stdscr.addnstr(y0 + 1 + len(body) + 1, x0, "╰" + "─" * (box_w - 2) + "╯", box_w, curses.A_REVERSE)
    stdscr.refresh()


def _handle_active_modal(stdscr, state: State, pairs: dict[str, int], row: SessionRow) -> Optional[tuple[str, Any]]:
    """Blocking modal on Enter for an active session. Returns a resume/attach action or None."""
    info = _gather_active_info(row)
    stdscr.nodelay(False)
    try:
        while True:
            _draw_active_modal(stdscr, row, info)
            c = stdscr.getch()
            if c in (27, ord('q')):
                state.msg = "· cancelled"
                return None
            if c == ord('t'):
                return _handle_tmux(stdscr, state)
            if c == ord('K'):
                _handle_kill(stdscr, state)
                return None
            if c == ord('v'):
                _tail_view(stdscr, state, pairs, row)
                return None
    finally:
        stdscr.nodelay(True)


def _handle_enter(stdscr, state: State, pairs: dict[str, int]) -> Optional[tuple[str, Any]]:
    """Enter key.
    * Idle session → return a resume action (caller chdir+execvp's the harness CLI)
    * Active session → show the active-session modal, which itself may return an action"""
    if not state.rows:
        return None
    row = state.rows[state.sel]
    if row.active:
        return _handle_active_modal(stdscr, state, pairs, row)
    adapter = state.adapters.get(row.harness)
    if adapter is None:
        state.msg = "· no adapter"
        return None
    argv = adapter.resume_argv(row)
    if not argv:
        state.msg = "· no resume_cmd configured"
        return None
    return ("resume", {"cwd": row.cwd, "argv": argv, "sid": row.sid, "harness": row.harness})


def _handle_new_tab(state: State) -> None:
    """`n` key: open the resume command in a new tab/pane of the user's terminal.

    Unlike Enter (which exits htv and execvp's the harness in place) and `t`
    (which uses tmux), this fires the configured `[new_tab] command` as a
    fire-and-forget subprocess so htv keeps running. Same active-session guard
    as Enter — we don't want a second resume to silently fork.
    """
    if not state.rows:
        return
    row = state.rows[state.sel]
    if row.active:
        state.msg = f"· active in pid {row.pid} — won't double-resume in a new tab"
        return
    cmd_template = state.cfg.new_tab_command
    if not cmd_template:
        state.msg = "· no new_tab.command configured — see config.example.toml"
        return
    adapter = state.adapters.get(row.harness)
    if adapter is None:
        state.msg = "· no adapter"
        return
    argv = adapter.resume_argv(row)
    if not argv:
        state.msg = "· no resume_cmd configured"
        return
    # `{resume}` is the resume argv joined as a shell-safe string so the user
    # can drop it into `sh -c {resume}` inside their new-tab command.
    from .adapters.base import _shell_quote
    placeholders = {
        "cwd": row.cwd,
        "sid": row.sid,
        "title": row.display_title,
        "harness": row.harness,
        "resume": " ".join(_shell_quote(a) for a in argv),
    }
    try:
        spawn = [s.format(**placeholders) for s in cmd_template]
    except KeyError as e:
        state.msg = f"· new_tab.command: unknown placeholder {e}"
        return
    try:
        # Fire-and-forget; htv keeps running. start_new_session detaches from
        # our process group so the spawned terminal command isn't killed if
        # htv exits.
        subprocess.Popen(spawn, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state.msg = f"· spawned new tab for {row.harness}:{row.sid[:8]}"
    except (FileNotFoundError, OSError) as e:
        state.msg = f"· new-tab failed: {e}"


def _handle_tags(stdscr, state: State) -> None:
    if not state.rows:
        return
    row = state.rows[state.sel]
    raw = _prompt_text(stdscr, "tags (comma-separated)", default=", ".join(row.tags))
    if raw is None:
        state.msg = "· cancelled"
        return
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    try:
        sidecar.save(row.jsonl, name=row.name, tags=tags)
    except OSError as e:
        state.msg = f"· save failed: {e}"
        return
    row.tags = tags
    state.msg = f"· tags: {', '.join(tags) or '(cleared)'}"


def _handle_tag_filter(stdscr, state: State) -> None:
    raw = _prompt_text(stdscr, "filter by tag (empty to clear)", default=state.tag_filter)
    if raw is None:
        state.msg = "· cancelled"
        return
    state.tag_filter = raw.strip()
    with state.lock:
        _apply_filters(state)
        state.sel = 0
    state.msg = f"· tag={state.tag_filter!r}" if state.tag_filter else "· filter cleared"


def _handle_tmux(stdscr, state: State) -> Optional[tuple[str, Any]]:
    """`t` key: attach to existing tmux pane (active sessions), or create a new
    tmux session + resume there (idle sessions)."""
    if not state.rows:
        return None
    row = state.rows[state.sel]

    # Active + in tmux → just switch/attach to that pane.
    if row.active and row.pid:
        pane = tmux_util.find_tmux_pane(row.pid)
        if pane:
            return ("tmux-attach", {"target": pane, "is_pane": True})
        # Active but bare tty — try the configured focus command.
        return _focus_bare_tty(state, row)

    # Idle → we'd create a new tmux session. Bail early if tmux isn't installed.
    if not tmux_util.have_tmux():
        state.msg = "· tmux not installed (try: brew install tmux  /  apt install tmux)"
        return None

    adapter = state.adapters.get(row.harness)
    if adapter is None:
        state.msg = "· no adapter"; return None
    argv = adapter.resume_argv(row)
    if not argv:
        state.msg = "· no resume_cmd configured"; return None
    default = (row.name or f"{row.harness}-{row.sid[:8]}").replace(" ", "-")
    name = _prompt_text(stdscr, "tmux session name", default=default)
    if not name:
        state.msg = "· cancelled"; return None
    # Sanitize: tmux session names can't contain . : or whitespace.
    name = name.replace(".", "-").replace(":", "-").replace(" ", "-")
    ok, msg = tmux_util.create_session(name, row.cwd, argv)
    if not ok:
        state.msg = f"· tmux: {msg}"; return None
    return ("tmux-attach", {"target": name, "is_pane": False})


def _focus_bare_tty(state: State, row: SessionRow) -> None:
    """Active session that isn't in tmux — resolve window id if needed, then run
    the configured focus command. Kitty's `--match pid:<N>` matches the window's
    own shell pid, NOT child harness processes — so we walk the window tree
    to find the owning window and substitute its id instead."""
    tty = tmux_util.tty_of(row.pid or 0)
    cmd = state.cfg.focus_command
    if not cmd:
        state.msg = f"· active in pid {row.pid} tty {tty or '?'} — no focus cmd configured"
        return None
    uses_win_id = any("{win_id}" in a for a in cmd)
    win_id = tmux_util.find_kitty_window(row.pid or 0) if uses_win_id else ""
    if uses_win_id and not win_id:
        state.msg = f"· focus: no kitty window owns pid {row.pid} (tty {tty or '?'})"
        return None
    placeholders = {
        "pid": str(row.pid or ""),
        "tty": tty,
        "title": row.display_title,
        "comm": row.harness,
        "win_id": win_id,
    }
    try:
        argv = [a.format(**placeholders) for a in cmd]
        import subprocess
        r = subprocess.run(argv, capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            state.msg = f"· focused pid {row.pid} (tty {tty or '?'})"
        else:
            state.msg = f"· focus failed: {(r.stderr or 'nonzero').strip()[:60]}"
    except Exception as e:
        state.msg = f"· focus error: {e}"
    return None


def _handle_search(stdscr, state: State) -> None:
    """/: enter live-search mode. _tui main loop routes subsequent keys to
    _live_search_step until the user hits Enter or Esc."""
    state.search_mode = True


def _live_search_step(state: State, c: int) -> None:
    """Handle one keystroke while in search_mode. Updates state.search_query
    live and re-applies filters on every edit. The lock guards against the
    background refresh worker swapping rows mid-filter.
    """
    if c in (10, 13, curses.KEY_ENTER):            # commit
        state.search_mode = False
        state.msg = (f"· search={state.search_query!r}  {len(state.rows)} match(es)"
                     if state.search_query else "· search cleared")
        return
    if c == 27:                                    # Esc — cancel + clear
        state.search_mode = False
        state.search_query = ""
        with state.lock:
            _apply_filters(state)
        state.msg = "· search cleared"
        return
    if c in (curses.KEY_BACKSPACE, 127, 8):
        state.search_query = state.search_query[:-1]
    elif 32 <= c < 127:
        state.search_query += chr(c)
    else:
        return                                     # ignore arrows / fn / ctrl
    with state.lock:
        _apply_filters(state)
        state.sel = 0


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
        # Give the OS a moment to reap, then ask the worker for an early refresh.
        time.sleep(0.3)
        state.wake.set()
    except Exception as e:
        state.msg = f"· kill failed: {e}"


def _tui(stdscr, cfg: AppConfig) -> Optional[tuple[str, Any]]:
    curses.curs_set(0)
    pairs = _init_colors(cfg)
    stdscr.nodelay(True)

    state = State(cfg=cfg)
    state.adapters = _instantiate_adapters(cfg)
    # Initial synchronous refresh so the first frame has rows.
    _refresh(state)
    # Background refresher — daemon thread so Ctrl-C / clean exit still kills it.
    worker = threading.Thread(target=_refresh_worker, args=(state,), name="htv-refresh", daemon=True)
    worker.start()

    tab_order = [TAB_ALL] + [h.name for h in cfg.harnesses if h.enabled]
    tab_keys = {ord(str(i + 1)): t for i, t in enumerate(tab_order)}

    try:
        return _event_loop(stdscr, state, pairs, tab_order, tab_keys)
    finally:
        state.stop.set()
        state.wake.set()  # unblock the worker so it can notice stop


def _event_loop(stdscr, state, pairs, tab_order, tab_keys) -> Optional[tuple[str, Any]]:
    """UI loop. Refreshes happen on a background thread; we never block on them.

    Threading model: the worker (a) does heavy work (ProcIndex + adapter scans),
    then (b) briefly takes state.lock to swap rows_all + rows + procs. The UI
    only takes the lock around _apply_filters() and _refresh-on-demand calls
    — i.e. when WE mutate the same fields. Read paths (_draw, indexing) are
    lock-free: Python name binding is atomic so reads either see the old or
    the new list reference, never a torn one. The only residual hazard is
    `state.rows[state.sel]` after a swap to a shorter list, which we clamp
    inline whenever we touch it.
    """
    while True:
        _draw(stdscr, state, pairs)
        state.msg = ""

        c = stdscr.getch()
        if c == -1:
            time.sleep(0.1); continue
        # Clamp sel in case the worker swapped in a shorter list since last frame.
        if state.rows:
            state.sel = min(state.sel, len(state.rows) - 1)
        # Live-search mode swallows all keys until Enter / Esc.
        if state.search_mode:
            _live_search_step(state, c)
            continue
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
            with state.lock:
                _apply_filters(state)
            state.msg = f"· show_active = {state.show_active}"
        elif c == ord('r'):
            _handle_rename(stdscr, state)
        elif c == ord('#'):
            _handle_tags(stdscr, state)
        elif c == ord('F'):
            _handle_tag_filter(stdscr, state)
        elif c == ord('/'):
            _handle_search(stdscr, state)
        elif c == 27:  # Esc — clear search → tag → (none)
            if state.search_query:
                state.search_query = ""
                with state.lock:
                    _apply_filters(state)
                state.msg = "· search cleared"
            elif state.tag_filter:
                state.tag_filter = ""
                with state.lock:
                    _apply_filters(state)
                state.msg = "· filter cleared"
        elif c == ord('v') and state.rows:
            _tail_view(stdscr, state, pairs, state.rows[state.sel])
            stdscr.nodelay(True)
        elif c == ord('t'):
            action = _handle_tmux(stdscr, state)
            if action is not None:
                return action
        elif c in (curses.KEY_ENTER, 10, 13):
            action = _handle_enter(stdscr, state, pairs)
            if action is not None:
                return action
        # `n` new-tab disabled until we have a reliable cross-terminal flow.
        # The handler (_handle_new_tab) and config (`[new_tab] command`) are
        # still wired; just not bound. Re-enable by uncommenting these lines.
        # elif c == ord('n'):
        #     _handle_new_tab(state)
        elif c == ord('K'):
            _handle_kill(stdscr, state)


def run_tui(cfg: AppConfig) -> Optional[tuple[str, Any]]:
    """Entry point. Returns an action tuple (e.g. ('resume', {...})) or None."""
    return curses.wrapper(_tui, cfg)


__all__ = ["run_tui"]
