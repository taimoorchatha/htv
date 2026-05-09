# htv

Harness session dashboard. One curses TUI for **Kiro**, **Claude Code**, and **pi** sessions — with tabs, live tail, smart attach, and sidecar labels. Adapter-based so [Codex](https://github.com/openai/codex) and friends drop in when they land.

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
- **Two take-over modes** (Enter vs `t`) — see [Enter vs t](#enter-vs-t-two-ways-to-take-over-a-session) below.
- **Never auto-forks** — active sessions can't be clobbered by a second resume.
- **Live tail view** of any session's JSONL, labeled USER / AI / TOOL, auto-follow.
- **Sidecar names + tags** per session — edit with `r` / `#`, filter across harnesses with `F`.
- **TOML config** at `~/.config/htv/config.toml` — swap session stores, resume commands, AI CLI, or window-focus command per-harness.
- **Read-only on upstream stores** — htv never writes to your kiro / claude / pi session files. Only its own `<base>.htv-meta.json` sidecars.

## Install

Python 3.11+ required (for `tomllib`). No runtime dependencies beyond stdlib.

### From source (recommended for now)

```sh
git clone https://github.com/taimoorchatha/htv ~/workplace/htv
ln -s ~/workplace/htv/bin/htv ~/bin/htv   # or anywhere on PATH
htv
```

### With pip

```sh
pip install git+https://github.com/taimoorchatha/htv
htv
```

First run drops a copy of [`config.example.toml`](config.example.toml) at `~/.config/htv/config.toml`.

## Enter vs t — two ways to take over a session

These keys do **different** things and cover complementary cases:

### Enter = "resume in my current terminal"

htv exits, `cd`s to the session's working directory, and execs the harness CLI (`kiro-cli --resume-id`, `claude --resume`, or `pi --session`) in place. You're **inside the session** in the same terminal. When you quit the session you land in your shell, not back in htv.

Only works on **idle** sessions — pressing Enter on an active session (●) is refused with a status line:

> `· active in pid 46473 — press K to kill first, or t to attach, or v to view`

This is deliberate. Claude's CLI silently forks when you re-resume an active session, producing orphan conversations. The guard prevents that.

### t = "take me to where it's running, or spin up a tmux session"

`t` branches on three cases:

| Session state | What `t` does |
|---|---|
| **Active + in a tmux pane** | `tmux switch-client` (inside tmux) or `tmux attach-session` (outside tmux) straight to that pane. You see the live session as it's running. |
| **Active + bare tty** | Runs the configured `focus.command` to focus the terminal window holding the session. Falls back to an info status if the command isn't configured or fs. |
| **Idle** | Prompts for a tmux session name (default: your session name or `<harness>-<sid>`), creates a detached tmux session running the resume argv in the session's cwd, then attaches. The session **keeps running after you detach** — unlike Enter's in-terminal resume. |

### What does "bare tty" mean?

A **bare tty** is a terminal process NOT running inside tmux. Directly in kitty / iTerm / gnome-terminal, in an ssh session, in a physical TTY, etc. The process attaches to a `/dev/pts/N` device without tmux's multiplexing layer on top.

Why it matters:

|  | Bare tty | tmux pane |
|---|---|---|
| Survives terminal close | ✗ | ✓ |
| Can be detached/reattached | ✗ | ✓ |
| htv can "jump" to it via `tmux switch-client` | ✗ | ✓ |
| htv can "focus the window" via `focus.command` | ✓ (WM-specific) | n/a |

htv detects which one your session is in by walking `/proc/<pid>/status` for `PPid` up to init, then cross-referencing against `tmux list-panes`. If any ancestor is a tmux pane's `pane_pid`, you're in tmux.

### Configuring `focus.command` for bare-tty sessions

Default assumes kitty with remote control enabled:

```toml
# ~/.config/htv/config.toml
[focus]
command = ["kitten", "@", "focus-window", "--match", "pid:{pid}"]
```

Requires `allow_remote_control yes` in `~/.config/kitty/kitty.conf`. Alternatives:

```toml
# X11 with wmctrl
command = ["wmctrl", "-a", "{title}"]

# Hyprland (Wayland)
command = ["hyprctl", "dispatch", "focuswindow", "pid:{pid}"]

# Disable — just show an info line
command = []
```

Placeholders available: `{pid}`, `{tty}`, `{title}`, `{comm}`.

## Keybindings

### List view

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Navigate |
| `1` `2` `3` `4` / `Tab` / `Shift-Tab` | Switch tabs (All / Kiro / Claude / Pi) |
| `Enter` | Resume selected session in **current** terminal |
| `t` | Attach to tmux pane / focus window / create new tmux session (see [Enter vs t](#enter-vs-t-two-ways-to-take-over-a-session)) |
| `v` | Live tail of the selected session's JSONL |
| `r` | Rename the session (saved in `<base>.htv-meta.json`) |
| `#` | Edit tags (comma-separated) |
| `F` | Filter list to sessions matching a tag |
| `Esc` | Clear tag filter |
| `a` | Toggle show-active |
| `K` | `SIGTERM` the process holding an active session |
| `q` | Quit |

### Tail view

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` / `PgUp` / `PgDn` | Scroll |
| `g` / `G` | Jump to top / bottom |
| `f` | Toggle auto-follow |
| `q` / `Esc` | Back to list |

## How it detects "active" sessions

- **Kiro** writes a `.lock` file with the holding PID — we trust it (fast, accurate).
- **Claude** and **pi** don't lock. htv walks `/proc/*/cwd` once per refresh and calls a session "active" if:
  1. The JSONL mtime is within `active_mtime_window_sec` (90s default), AND
  2. A live `claude` or `pi` process has the same cwd.

Short false-positive window (process exited < 90s ago) but cheap enough to run every refresh.

## Sidecar metadata

htv never writes to upstream session files. Per-session user data lives beside the JSONL:

```
~/.pi/agent/sessions/.../<ts>_<uuid>.jsonl           ← pi owns this
~/.pi/agent/sessions/.../<ts>_<uuid>.htv-meta.json   ← we own this
```

Contents:

```json
{
  "name": "fix cagg timeout",
  "tags": ["oncall", "s2p"],
  "updated_at": "2026-05-09T07:30:00Z"
}
```

Display precedence: user `name` → AI title (step 6) → raw title → `(no title)`. Deleting the jsonl deletes the sidecar along with it (same directory).

## Configuration

See [`config.example.toml`](config.example.toml) for the full schema. Per-harness overrides:

- `session_dir` / `projects_dir` / `sessions_dir` — where that store lives
- `resume_cmd` — templated with `{sid}`, `{cwd}`, `{jsonl}`
- `enabled = false` — hide a harness entirely
- `label`, `color` — appearance in the tab bar and list

Add a brand-new harness: write a `~200-line` adapter in `htv_app/adapters/`, register it, drop a `[harnesses.<name>]` block in your config.

## Code-quality gate

`bin/review` enforces, as pre-commit + pre-push hooks:

- All `.py` files compile
- pyflakes clean (with a small allow-list for deliberate side-effect imports)
- No function ≥ 100 LOC (warns at 60)
- No duplicate function bodies across files
- **150 LOC cap per commit** — separately for production code and test code

Rule lives in `~/.claude/CLAUDE.md` / `~/.kiro/steering.md` / pi memory; see the sibling `agent-config` repo for the system-wide version.

Install hooks in a fresh clone:

```sh
./bin/install-hooks
```

## Roadmap

- [x] scaffold, config loader, adapter protocol, process index
- [x] kiro + claude + pi adapters, JSONL parsers, cwd detection
- [x] curses TUI: tabs, list, smart Enter, tail view, pulsing activity
- [x] sidecar names + tags, rename (`r`), tag-edit (`#`), tag filter (`F`)
- [x] tmux smart-attach (`t`) + `focus.command` for bare-tty sessions
- [ ] AI title worker + `/` semantic search
- [ ] codex adapter (when codex-cli ships)
- [ ] optional: watch filesystem for new sessions instead of polling
- [ ] optional: demo GIF via [vhs](https://github.com/charmbracelet/vhs)

## License

MIT. See [LICENSE](LICENSE).
