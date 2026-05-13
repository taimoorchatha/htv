# htv - (harnesstv)

> **htop for your AI coding sessions.** One terminal dashboard for every Claude Code, pi, and Kiro session running on your machine.

<!-- TODO: replace this static screenshot with the recorded demo GIF (issue #10). -->

```
┌─ htv 0.1 ────────────────────────────────────────────────────────────────────┐
│  All (185)  Claude (161)  pi (24)  Kiro (0)                                  │
│  ST H   AGE  MSG   CWD                          TITLE                        │
│  ● pi   2s   251  ~/code/api-server           fix the rate-limiter regress…  │
│  ● CC  19h   176  ~/code/web                  ship the onboarding redesign   │
│  ● pi  19h   169  ~/code/infra/terraform      debug the staging vpc peering  │
│  · pi   1d   100  ~/code/data-pipeline        backfill last quarter's events │
│  · pi   2d    27  ~/code/cli-tools            add --json flag to the report  │
│  · CC   2d   140  ~/code/mobile               investigate the ios crash log  │
│  · CC   4d   659  ~/code/web                  [Request interrupted by user]  │
│  · pi   4d   155  ~/notes                     (no title)                     │
│                                                                              │
│  185 shown · 3 active                                                        │
│  ↑↓ nav  ⏎ resume  n new-tab  t tmux  v view  / search  r rename  q quit     │
└──────────────────────────────────────────────────────────────────────────────┘

  ● = live process holding this session    · = idle
```

## Why

If you use AI coding agents across many directories, you've already lost track of your sessions. They're all somewhere, but you don't remember where, and `claude --resume` on one that's *already running* silently forks it.

htv answers "what's running where?" One list, every directory, every harness, live activity, with a guard that refuses to re-resume an already-running session.

## What you get

- **Every session on your machine in one list.** Sorted by recency, filterable by harness, searchable as you type. The 3-day-old chat you forgot about in `~/code/infra` is right there.
- **No accidental forks.** A live session shows a `●` and a modal on Enter with pid/tty/tmux/window info, so you take over the existing session instead of starting a duplicate.
- **One keystroke to take it over.** `Enter` to resume in this terminal, `n` to open it in a new tab, `t` to jump to its existing tmux pane or terminal window. `v` to read the conversation without opening it.
- **Multi-harness from day one.** Claude Code, pi, Kiro; Codex when it ships. Adapter-based, ~200 LOC to add a new one.

## Install

Python 3.11+ (for `tomllib`). Stdlib only, no runtime dependencies.

```sh
pip install --user git+https://github.com/taimoorchatha/htv
htv
```

Or from source:

```sh
git clone https://github.com/taimoorchatha/htv ~/workspace/htv
ln -s ~/workspace/htv/bin/htv ~/bin/htv
htv
```

First run drops [`config.example.toml`](config.example.toml) at `~/.config/htv/config.toml`.

### `htv: command not found` after `pip install`?

Pip installs the `htv` script into your Python user-base `bin/` directory, which isn't always on `$PATH`. Find the right directory and add it:

```sh
# Show where pip put it:
python3 -m site --user-base
```

Append `/bin` to that path and add it to your shell rc. The common defaults:

| Environment | Add to `~/.zshrc` / `~/.bashrc` |
|---|---|
| **macOS, Python.org installer** | `export PATH="$HOME/Library/Python/3.13/bin:$PATH"` |
| **macOS, Homebrew Python** | `export PATH="$(brew --prefix)/opt/python@3.13/libexec/bin:$PATH"` (or just `$(brew --prefix)/bin` if you used `brew install python`) |
| **Linux** | `export PATH="$HOME/.local/bin:$PATH"` |

*(Fittingly, you can also just paste this whole section into your AI of choice and have it figure out which row is yours.)*

Then `source ~/.zshrc` (or open a new terminal) and `htv` should resolve. Run `htv doctor` to confirm — it prints config + adapter status without entering the TUI.

## Platform support

Works on **Linux** and **macOS**. macOS uses `ps` + batched `lsof` instead of `/proc` (~590ms per scan, on a background thread so the UI never blocks).

For `t` (focus the terminal window of an active session), htv shells out to a command you configure in `[focus]`. Kitty has surgical window targeting via `kitten @ ls`; iTerm2 targets the exact session by `tty`; Ghostty / Terminal.app currently activate the app only. Hyprland, Sway, X11/wmctrl, and GNOME (with [Window Calls](https://github.com/ickyicky/window-calls)) work in theory, recipes for every terminal/WM live in [`config.example.toml`](config.example.toml).

## Enter, n, t, three ways to take over

- **`Enter`**, resume in the *current* terminal. htv `exec`s the harness CLI in place; you're inside the session in the same window. When you quit you land in your shell, not back in htv.
- **`n`**, spawn the resume command in a *new* tab/pane (fire-and-forget; htv keeps running). Configurable per terminal in `[new_tab]`.
- **`t`**, go to where it's already running. If the session is in a tmux pane, `tmux switch-client` to it. If it's a bare tty (no tmux), run your `focus.command` to focus that terminal window. If the session is idle, spawn a detached tmux session running the resume argv.

All three refuse to re-resume an already-active session. On Active sessions, `Enter` shows a modal so you see what's actually holding it:

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

This prevents the silent-fork bug where `claude --resume <sid>` on an already-running session creates an orphan conversation.

## Keybindings

### List view

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Navigate |
| `1` `2` `3` `4` / `Tab` / `Shift-Tab` | Switch tabs (All / Kiro / Claude / Pi) |
| `Enter` | Idle: resume in current terminal. Active: open the modal above. |
| `n` | Open the resume command in a new tab/pane of your terminal (configurable per terminal, see [Configuration](#configuration)) |
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

## How it works

- **Active detection.** Kiro writes a `.lock` file with the holding PID, we trust it. Claude and pi don't lock, so htv builds a per-refresh process index (Linux: `/proc/*/cwd`; macOS: `ps -axo pid=,comm=` + batched `lsof`) and matches by cwd. For each cwd with a live harness process, the newest jsonl in that cwd is marked active.
- **Refresh is on a background thread**, so a 600ms-on-macOS scan never blocks the UI. Keypress-to-redraw latency is ~50ms.
- **Per-jsonl row counts are cached** keyed on `(mtime_ns, size)` so we don't re-parse 16-MB conversation files every tick.
- **Read-only on upstream stores.** User-defined names and tags live in a `<base>.htv-meta.json` sidecar next to the JSONL. Delete the jsonl, the sidecar goes with it.

## Configuration

See [`config.example.toml`](config.example.toml) for the full schema. The flags worth knowing:

- `resume_cmd` (per harness), templated with `{sid}`, `{cwd}`, `{jsonl}`.
- `resume_via_shell = true` (per harness), set this if your harness binary lives behind nvm / asdf / mise / pyenv / rbenv. htv will exec `$SHELL -i -c 'exec ...'` instead of calling `execvp` directly, so lazy-loaders and shell functions fire first. *Symptom this fixes: pressing `Enter` prints `not found: 'pi'` even though `which pi` works in that terminal.*
- `[focus] command`, what to run when `t` needs to focus the terminal of an active bare-tty session. Placeholders: `{pid}` `{tty}` `{title}` `{comm}` `{win_id}`.
- `[new_tab] command`, what to run when `n` spawns a new tab. Placeholders: `{cwd}` `{sid}` `{title}` `{harness}` `{resume}` (the resume argv shell-quoted, ready for `sh -c`).

Recipes for kitty / iTerm2 / Ghostty / Terminal.app / tmux / Hyprland / Sway / X11 live in [`config.example.toml`](config.example.toml).

## Adding a harness

~200 LOC in `htv_app/adapters/`. Implement `list_sessions(procs) -> [SessionRow]` and `tail_entries(row) -> [(kind, text)]`, register it, drop a `[harnesses.<name>]` block in your config. See `htv_app/adapters/pi.py` for a worked example.

## Roadmap

Open on the [tracker](https://github.com/taimoorchatha/htv/issues):

| Priority | Issue |
|---|---|
| P1 | [#3](https://github.com/taimoorchatha/htv/issues/3) Summarize-this-session on demand (ask-htv) |
| P2 | [#8](https://github.com/taimoorchatha/htv/issues/8) codex adapter · [#4](https://github.com/taimoorchatha/htv/issues/4) sort by column · [#5](https://github.com/taimoorchatha/htv/issues/5) tail-view live-search |
| P3 | [#2](https://github.com/taimoorchatha/htv/issues/2) AI titles · [#7](https://github.com/taimoorchatha/htv/issues/7) search conversation content · [#9](https://github.com/taimoorchatha/htv/issues/9) fs watcher · [#6](https://github.com/taimoorchatha/htv/issues/6) tail title searchable |
| P4 | [#10](https://github.com/taimoorchatha/htv/issues/10) demo GIF |

## License

MIT. See [LICENSE](LICENSE).
