"""Opens sessions as kitty windows on workspace 3, backed by per-session tmux.

Each claude process lives in its own tmux session (`cv-<sid8>`) so it survives a
compositor restart; kitty is only a viewer attached to it. One tmux session per
window avoids the mirrored-view problem of attaching several clients to one.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .models import LaunchMode, Session, SessionKind

WINDOW_CLASS = "claudev-session"
CLAUDEV_BIN = Path.home() / ".local" / "bin" / "claudev"
TMUX_PREFIX = "cv-"


class LaunchError(RuntimeError):
    """Raised when a session cannot be opened."""


def tmux_name(sid: str) -> str:
    return f"{TMUX_PREFIX}{sid[:8]}"


def _run(cmd: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def tmux_alive(name: str) -> bool:
    return _run(["tmux", "has-session", "-t", f"={name}"]).returncode == 0


def list_tmux_sessions() -> list[str]:
    out = _run(["tmux", "list-sessions", "-F", "#{session_name}"]).stdout
    return [line for line in out.splitlines() if line.startswith(TMUX_PREFIX)]


def ensure_tmux(session: Session) -> str:
    """Create the session's tmux holder if absent; returns its name."""
    if not session.resumable:
        raise LaunchError(f"{session.sid}: призрак не резюмируется — сначала материализуй (m)")
    name = tmux_name(session.sid)
    if tmux_alive(name):
        return name

    cwd = session.project if session.project.is_dir() else Path.home()
    flag = "--attach-plain" if session.launch_mode is LaunchMode.CLAUDE else "--attach"
    inner = f"{shlex.quote(str(CLAUDEV_BIN))} {flag} {shlex.quote(session.sid)}"
    result = _run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(cwd), "bash", "-lc", inner],
        timeout=20,
    )
    if result.returncode != 0:
        raise LaunchError(f"tmux не поднял сессию: {result.stderr.strip()}")
    _run(["tmux", "set-option", "-t", name, "status", "off"])
    _run(["tmux", "set-option", "-t", name, "destroy-unattached", "off"])
    return name


def _hyprland_up() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def _clients_of_class() -> list[str]:
    if not _hyprland_up():
        return []
    out = _run(["hyprctl", "-j", "clients"]).stdout
    try:
        import json

        return [c["address"] for c in json.loads(out) if c.get("class") == WINDOW_CLASS]
    except (ValueError, KeyError, TypeError):
        return []


def open_window(session: Session, *, workspace: int = 3) -> str:
    """Attach a kitty viewer on the target workspace; returns the tmux name."""
    name = ensure_tmux(session)
    title = f"{session.project_label} · {session.title[:40]}"
    cmd = [
        "kitty",
        "--class", WINDOW_CLASS,
        "--title", f"[{name}] {title}",
        "--directory", str(session.project if session.project.is_dir() else Path.home()),
        # Long sessions blow past the global 2000-line scrollback.
        "--override", "scrollback_lines=50000",
        "--override", "confirm_os_window_close=0",
        "tmux", "attach-session", "-t", f"={name}",
    ]
    if _hyprland_up():
        _run(["hyprctl", "dispatch", "workspace", str(workspace)])
        _run(["hyprctl", "dispatch", "exec", "[workspace %d silent]" % workspace + " " +
              " ".join(shlex.quote(c) for c in cmd)])
    else:
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return name


def focus_existing(session: Session) -> bool:
    """Focus an already-open viewer for this session, if any."""
    if not _hyprland_up():
        return False
    import json

    out = _run(["hyprctl", "-j", "clients"]).stdout
    try:
        clients = json.loads(out)
    except ValueError:
        return False
    name = tmux_name(session.sid)
    for client in clients:
        if client.get("class") != WINDOW_CLASS:
            continue
        if name in str(client.get("title", "")) or session.sid[:8] in str(client.get("title", "")):
            _run(["hyprctl", "dispatch", "focuswindow", f"address:{client['address']}"])
            return True
    return False


def resume_command(session: Session) -> str:
    """Shell one-liner that resumes this session — used for clipboard fallback."""
    cwd = session.project if session.project.is_dir() else Path.home()
    flag = "--attach-plain" if session.launch_mode is LaunchMode.CLAUDE else "--attach"
    return f"cd {shlex.quote(str(cwd))} && claudev {flag} {session.sid}"


def copy_to_clipboard(text: str) -> bool:
    for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            proc = subprocess.run(tool, input=text, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    return False


def kill_session(session: Session) -> bool:
    """Stop the tmux holder; the registry record disappears with a clean exit."""
    name = tmux_name(session.sid)
    if not tmux_alive(name):
        return False
    return _run(["tmux", "kill-session", "-t", f"={name}"]).returncode == 0
