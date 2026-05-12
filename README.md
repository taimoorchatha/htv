# htv

Harness session dashboard. One curses TUI for **Kiro**, **Claude Code**, and **pi** sessions — with tabs, live fuzzy search, smart attach, and sidecar labels. Adapter-based so [Codex](https://github.com/openai/codex) and friends drop in when they land.

Runs on **Linux** today · **macOS** compatible in theory with known caveats (see [Platform support](#platform-support)).

```
 All (122)  Kiro (81)  Claude (4)  Pi (37)
 ST H   AGE  MSG   CWD                          TITLE
  ● pi  2s   251   ~/vault/s2p/ai-champion      shipping htv v0.1
  · K   1d   2284  ~/vault/s2p/fiml             debugging cagg timeouts
  ● K   12m  8     ~                            quick brazil question
  · CC  2h   281   ~                            improving claude code experience
```

## Features

- **One dashboard** for every coding-agent session on the machine.
- **Pulsing activity glyph** (● ↔ ○) so live sessions visibly beat on-screen.
- **Two take-over modes** (Enter vs `t`) — see [Enter vs t](#enter-vs-t).
- **Never auto-forks** — active sessions show a modal with pid/tty/tmux/window info instead of blindly resuming.
- **Live tail view** of any session's JSONL, labeled USER / AI / TOOL, auto-follow.
- **Live fuzzy search** (`/`) — as you type, list narrows; matched characters bold. Subsequence matching (`kcgt` → **K**ell's **c**a**g**g **t**imeout).
- **Sidecar names + tags** per session — edit with `r` / `#`, filter with `F`.
- **TOML config** at `~/.config/htv/config.toml`.
- **Read-only on upstream stores** — htv never writes to kiro / claude / pi session files. Only its own `<base>.htv-meta.json` sidecars.

## Install

Python 3.11+ (for `tomllib`). Stdlib only — no runtime dependencies.

### From source

```sh
git clone https://github.com/taimoorchatha/htv ~/workplace/htv
ln -s ~/workplace/htv/bin/htv ~/bin/htv
htv
```

### With pip

```sh
pip install git+https://github.com/taimoorchatha/htv
htv
```

First run drops [`config.example.toml`](config.example.toml) at `~/.config/htv/config.toml`.

## Platform support

### OS

| OS | Status | Tested in practice | Notes |
|---|---|---|---|
| **Linux** | ✓ full | ✓ Ubuntu + kitty | Primary target |
| **macOS** | partial | ✗ not yet | Kiro works (uses lock files). Claude/pi active detection requires a `ps`-based fallback to `/proc` ([#13](https://github.com/taimoorchatha/htv/issues/13)). The app still runs — it just marks Claude/pi sessions as idle when they aren't. |

### Terminals — for `t` / `focus.command`

The mechanism is config-driven (any command works), but the precision depends on what each terminal exposes:

| Terminal | Precision | Supported in theory | Tested in practice |
|---|---|---|---|
| **kitty** (any OS) | window-surgical | ✓ `kitten @ focus-window --match id:{win_id}` | ✓ Linux |
| **Hyprland** (Wayland) | pid-surgical | ✓ `hyprctl dispatch focuswindow pid:{pid}` | ✗ |
| **Sway** (Wayland) | pid-surgical | ✓ `swaymsg '[pid={pid}]' focus` | ✗ |
| **X11** + any terminal | pid-surgical | ✓ `sh -c 'xdotool windowactivate $(xdotool search --pid {pid}` head -1`)'` | ✗ |
| **iTerm2** (macOS) | app-level | partial — `osascript -e 'tell application "iTerm2" to activate'`. Tab targeting: [#12](https://github.com/taimoorchatha/htv/issues/12) | ✗ |
| **Terminal.app** (macOS) | app-level | partial — `osascript -e 'tell application "Terminal" to activate'`. Tab targeting: [#12](https://github.com/taimoorchatha/htv/issues/12) | ✗ |
| **GNOME/Wayland** | app-level | partial — needs a gnome-shell extension like [Window Calls](https://github.com/ickyicky/window-calls) for precise focus | ✗ |
| **raw Linux VT** (tty1-6) | ✗ | not focusable from userspace (`chvt` needs root) | ✗ |

Drop your terminal's recipe into `~/.config/htv/config.toml` under `[focus]`. Placeholders available: `{pid}`, `{tty}`, `{title}`, `{comm}`, `{win_id}` (kitty only).

## Enter vs t

### Enter = "resume in my current terminal"

**Idle session**: htv exits, `cd`s to the session's cwd, and execs the harness CLI (`kiro-cli --resume-id`, `claude --resume`, `pi --session`) in place. You're inside the session in the same terminal. When you quit, you land in your shell — not back in htv.

**Active session** (●): Enter shows a modal instead of blindly resuming, to prevent silent forks (Claude in particular will fork if you re-resume an active session):

```
╭─ already running ─────────────────────────────╮
│ Session:   fix cagg timeout                   │
│ Harness:   pi                                 │
│                                               │
│ Running in pid 46473                          │
│ tty:       /dev/pts/17                        │
│ in tmux:   no (bare tty)                      │
│ kitty win: 1                                  │
│                                               │
│ What do you want to do?                       │
│   [t]    tmux attach / focus the window       │
│   [K]    kill pid 46473 (then Enter to resume) │
│   [v]    view conversation tail (read-only)   │
│   [esc]  cancel                               │
╰───────────────────────────────────────────────╯
```

### t = "take me to where it's running, or spin up a tmux session"

| Session state | What `t` does |
|---|---|
| **Active + in a tmux pane** | `tmux switch-client` (inside tmux) or `tmux attach-session` (outside) straight to that pane. |
| **Active + bare tty** | Runs the configured `focus.command` to focus the terminal window. For kitty: resolves the owning kitty window via `kitten @ ls` (walking the foreground-processes tree) and focuses it. Falls back to an info line if the command or tooling isn't available. |
| **Idle** | Prompts for a tmux session name, creates a detached tmux session running the resume argv in the session's cwd, then attaches. The session **keeps running after you detach** — unlike Enter's in-terminal resume. |

### What does "bare tty" mean?

A **bare tty** is a terminal process NOT running inside tmux — directly in kitty / iTerm / gnome-terminal, in an ssh shell, a physical TTY, etc. It attaches to `/dev/pts/N` without tmux's multiplexing layer on top.

|  | Bare tty | tmux pane |
|---|---|---|
| Survives terminal close | ✗ | ✓ |
| Can be detached/reattached | ✗ | ✓ |
| htv can "jump" to it via `tmux switch-client` | ✗ | ✓ |
| htv can "focus the window" via `focus.command` | ✓ (WM-/terminal-specific) | n/a |

## Keybindings

### List view

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Navigate |
| `1` `2` `3` `4` / `Tab` / `Shift-Tab` | Switch tabs (All / Kiro / Claude / Pi) |
| `Enter` | Idle: resume in current terminal. Active: open the modal above. |
| `t` | Tmux attach / focus window / create new tmux session |
| `v` | Live tail of the selected session's JSONL |
| `/` | Live fuzzy search (name + title + cwd). Type to filter in real time. |
| `r` | Rename the session (sidecar) |
| `#` | Edit tags (comma-separated) |
| `F` | Filter list to sessions matching a tag |
| `Esc` | Clear the most recent filter (search → tag → none) |
| `a` | Toggle show-active |
| `K` | `SIGTERM` the process holding an active session |
| `q` | Quit |

### Tail view

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` / `PgUp` / `PgDn` | Scroll |
| `g` / `G` | Jump to top / bottom |
| `f` | Toggle auto-follow |
| `/` | Search within the conversation |
| `n` / `N` | Next / previous match |
| `q` / `Esc` | Back to list |

## How it detects "active" sessions

- **Kiro** writes a `.lock` file with the holding PID — we trust it (fast, accurate, cross-platform).
- **Claude** and **pi** don't lock. On Linux, htv walks `/proc/*/cwd` per refresh and calls a session "active" if:
  1. The JSONL mtime is within `active_mtime_window_sec` (90s default), AND
  2. A live `claude` or `pi` process has the same cwd.

On **macOS** the `/proc` scan short-circuits (silently returns empty). Claude/pi will render as idle even when live. Fix tracked in [#13](https://github.com/taimoorchatha/htv/issues/13).

## Sidecar metadata

htv never writes to upstream session files. Per-session user data lives beside the JSONL:

```
~/.pi/agent/sessions/.../<ts>_<uuid>.jsonl           ← pi owns this
~/.pi/agent/sessions/.../<ts>_<uuid>.htv-meta.json   ← we own this
```

```json
{
  "name": "fix cagg timeout",
  "tags": ["oncall", "s2p"],
  "updated_at": "2026-05-09T07:30:00Z"
}
```

Display precedence: user `name` → AI title ([#2](https://github.com/taimoorchatha/htv/issues/2), deferred) → raw title → `(no title)`. Deleting the jsonl deletes the sidecar with it.

## Configuration

See [`config.example.toml`](config.example.toml) for the full schema. Per-harness overrides:

- `session_dir` / `projects_dir` / `sessions_dir` — where the store lives
- `resume_cmd` — templated with `{sid}`, `{cwd}`, `{jsonl}`
- `enabled = false` — hide a harness entirely
- `label`, `color` — appearance in the tab bar and list

Add a brand-new harness: write a `~200-line` adapter in `htv_app/adapters/`, register it, drop a `[harnesses.<name>]` block in your config.

## Code-quality gate

`bin/review` runs as pre-commit + pre-push hooks:

- All `.py` files compile
- pyflakes clean (with a small allow-list for deliberate side-effect imports)
- No function ≥ 100 LOC (warns at 60)
- No duplicate function bodies across files
- **150 LOC cap per commit** — separately for `.py`/`.ts`/etc. production code and test code (markdown / config / shell ignored)

Rule documented in [`agent-config`](../../taimoorchatha/agent-config) and replicated in `~/.claude/CLAUDE.md` / `~/.kiro/steering.md`. Install hooks in a fresh clone:

```sh
./bin/install-hooks
```

## Roadmap

- [x] Scaffold, config loader, adapter protocol, process index
- [x] kiro + claude + pi adapters
- [x] Curses TUI — tabs, smart Enter, tail view, pulsing activity
- [x] Sidecar names + tags + tag filter
- [x] Tmux smart-attach + `focus.command` + kitty window resolver
- [x] Live fuzzy search with bold-highlight, subsequence matching
- [x] Active-session modal on Enter

**Open** (see [issues](https://github.com/taimoorchatha/htv/issues)):

| Priority | Issue |
|---|---|
| P1 | [#3](https://github.com/taimoorchatha/htv/issues/3) Summarize-this-session on demand (ask-htv, needs auth flow) |
| P1 | [#11](https://github.com/taimoorchatha/htv/issues/11) Resume fails under nvm/asdf/mise lazy-loaders |
| P2 | [#8](https://github.com/taimoorchatha/htv/issues/8) codex adapter · [#4](https://github.com/taimoorchatha/htv/issues/4) sort by column · [#5](https://github.com/taimoorchatha/htv/issues/5) tail-view live-search · [#13](https://github.com/taimoorchatha/htv/issues/13) macOS active detection |
| P3 | [#2](https://github.com/taimoorchatha/htv/issues/2) AI titles (deferred) · [#7](https://github.com/taimoorchatha/htv/issues/7) search conversation content · [#6](https://github.com/taimoorchatha/htv/issues/6) tail title searchable · [#9](https://github.com/taimoorchatha/htv/issues/9) fs watcher · [#12](https://github.com/taimoorchatha/htv/issues/12) macOS focus helpers |
| P4 | [#10](https://github.com/taimoorchatha/htv/issues/10) demo GIF |

## License

MIT. See [LICENSE](LICENSE).
