# claudev

Session persistence for Claude Code. A session becomes a record on disk plus a
tmux holder, so closing the terminal, restarting the compositor or rebooting the
machine does not end the conversation.

## Parts

| File | Role |
|---|---|
| `bin/claudev` | Launcher. Registers the session, refuses duplicates, execs the tuned wrapper. |
| `bin/claudev-restore` | Re-attaches a viewer window per holder and groups them on one workspace. |
| `bin/claudev-headless.sh` | Boot-time holder spawner for a systemd user unit — runs before any compositor. |
| `bin/claudev-pick`, `bin/claudev-pick-window` | Picker TUI entry points. |
| `bin/claudev-patch-resume` | Byte patch so the `/resume` picker copies `claudev`, not bare `claude`. |
| `bin/claudev-backup` | rsync mirror of transcripts to a remote host, plus dated hardlink snapshots. |
| `tui/` | The picker itself: every project, favourites, tags, preview, reconstruction of sessions whose transcript is gone. |

## Design notes

- **Registry**: `~/.local/state/claudev/sessions/<sid>`, line 1 = cwd, line 2 = mode.
  A clean exit removes the record; a reboot or system kill keeps it. `HUP` during
  shutdown is distinguished from `HUP` on window close, so a reboot never drops work.
- **One process per session id.** Two agents on one transcript append independent
  uuid/parentUuid chains and tangle the conversation tree, so the launcher refuses
  and points at the live copy.
- **No lock around holder creation.** `tmux new-session` is atomic, and a lock's
  file descriptor would leak into the tmux server and never be released.
- **Transcripts are deleted after 30 days** by the agent's own retention sweep
  (`cleanupPeriodDays`). `/home` on ext4 has no snapshots, so `claudev-backup` is
  the only thing that can bring a session back. Re-check the setting after every
  agent upgrade.

## Install

```bash
install -m755 bin/* ~/.local/bin/
pipx install ./tui            # or: python -m venv .venv && .venv/bin/pip install ./tui
export CLAUDEV_WRAPPED=/path/to/prompt-template/claude-wrapped
```

Backup target is configuration, not a constant:

```bash
CLAUDEV_BACKUP_HOSTS="vpn.internal vps.example.com" \
CLAUDEV_BACKUP_PORT=22 CLAUDEV_BACKUP_USER=me ~/.local/bin/claudev-backup
```

## Status

`claudev-patch-resume` matches the minified literal structurally rather than by
offset, but the literal it targets is gone in Claude Code 2.1.257 — the patch
reports `no-match` there and skips. Everything else is version-independent.
