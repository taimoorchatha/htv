# htv

Harness session dashboard. One curses TUI for **Kiro**, **Claude Code**, and **pi** sessions — with tabs, live tail, and smart resume. Adapter-based so [Codex](https://github.com/openai/codex) and friends drop in when they land.

```
 All (122)  Kiro (81)  Claude (4)  Pi (37)
 ST H  AGE  MSG   CWD                          TITLE
  ● pi 2s   251   ~/vault/s2p/ai-champion      shipping htv v0.1
  · K  1d   2284  ~/vault/s2p/fiml             debugging cagg timeouts
  ● K  12m  8     ~                            quick brazil question
  · CC 2h   281   ~                            improving claude code experience
```

- **One dashboard** for every coding-agent session on the machine.
- **Smart resume** — Enter on an idle session `cd`s to the right dir and execs `<cli> --resume <sid>`. You replace `htv` with the harness CLI in the same terminal; when the session ends, you're back at your shell.
- **Active sessions don't auto-fork** — Enter refuses to resume a session already held by another process (prevents Claude's silent fork behavior).
- **Live tail view** on any session — stream the JSONL as USER / AI / TOOL labelled lines.
- **Pulsing activity glyph** so live sessions visibly beat on-screen.
- **TOML config** at `~/.config/htv/config.toml` — swap session stores, resume commands, or AI CLI per-harness.
- **Read-only on upstream stores** — never writes to your kiro / claude / pi session files.

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

## Keybindings

| Key | Action |
|-----|--------|
| `↑` `↓` / `j` `k` | Navigate |
| `1` `2` `3` `4`  / `Tab` / `Shift-Tab` | Switch tabs (All / Kiro / Claude / Pi) |
| `Enter` | Resume selected session in the current terminal (exec handoff) |
| `v` | View the live tail of the selected session |
| `a` | Toggle show-active |
| `r` | Refresh |
| `K` | `SIGTERM` the process holding an active session |
| `q` | Quit |

Within the tail view: `↑` `↓` scroll, `g` / `G` jump, `f` toggle follow, `q` back.

## How it detects "active" sessions

- **Kiro** uses `.lock` files — we trust those (fast, accurate).
- **Claude** and **pi** don't lock, so `htv` walks `/proc/*/cwd` once per refresh and calls a session "active" if (a) its JSONL mtime is recent and (b) a matching `claude`/`pi` process has the same cwd.

## Configuration

See [`config.example.toml`](config.example.toml) for the full schema. Things you can override per-harness:

- `session_dir` / `projects_dir` / `sessions_dir` — where that store lives
- `resume_cmd` — templated with `{sid}`, `{cwd}`, `{jsonl}`
- `enabled = false` — hide a harness entirely
- `label` and `color` — how it appears in the tab bar and list

Add a brand-new harness by writing one `~200-line` adapter in `htv_app/adapters/`, registering it, and adding a `[harnesses.<name>]` block to your config.

## Roadmap

- [x] scaffold, config loader, adapter protocol, process index
- [x] kiro + claude + pi adapters, JSONL parsers, cwd detection
- [x] curses TUI: tabs, list, smart Enter, tail view, pulsing activity
- [ ] session names + tags (sidecar `<sid>.htv-meta.json`)
- [ ] tag filter (`F`), rename (`r`), tag-edit (`#`)
- [ ] tmux-aware attach (`t`) + configurable window-focus command
- [ ] AI title worker + `/` semantic search
- [ ] codex adapter (when codex-cli ships)

## License

MIT. See [LICENSE](LICENSE).
