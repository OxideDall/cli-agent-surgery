"""Builds the session index from ~/.claude, without parsing whole transcripts.

Transcripts reach megabytes, so per-session metadata comes from a head scan, a
tail scan and a byte-level count; results are cached by (size, mtime).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import LaunchMode, RunState, Session, SessionKind

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
HISTORY_FILE = CLAUDE_HOME / "history.jsonl"
STATE_DIR = Path.home() / ".local" / "state" / "claudev"
REGISTRY_DIR = STATE_DIR / "sessions"
CACHE_FILE = STATE_DIR / "index-cache.json"
MATERIALIZED_DIR = STATE_DIR / "materialized"

_SCAN_WINDOW = 256 * 1024
_CACHE_VERSION = 3
_SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _parse_ts(raw: object) -> datetime | None:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 1e11 else raw, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _flatten_content(content: object) -> str:
    """Message content is either a string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(str(block.get("text", "")))
        elif block.get("type") == "thinking":
            out.append(str(block.get("thinking", "")))
    return "\n".join(out)


def _iter_json_lines(blob: bytes, *, drop_first: bool) -> list[dict]:
    """Parse whole JSON lines out of a byte window; partial edge lines dropped."""
    lines = blob.split(b"\n")
    if drop_first and lines:
        lines = lines[1:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


@dataclass(slots=True)
class _Probe:
    """Metadata recovered from one transcript file."""

    title: str = ""
    last_prompt: str = ""
    first_prompt: str = ""
    cwd: str = ""
    git_branch: str | None = None
    model: str | None = None
    tokens: int = 0
    started: datetime | None = None
    updated: datetime | None = None
    user_turns: int = 0
    assistant_turns: int = 0


def _count_turns(path: Path) -> tuple[int, int]:
    user = assistant = 0
    with path.open("rb") as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            user += chunk.count(b'"type":"user"')
            assistant += chunk.count(b'"type":"assistant"')
    return user, assistant


def probe_transcript(path: Path) -> _Probe:
    """Read head+tail of a transcript for display metadata."""
    probe = _Probe()
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(min(size, _SCAN_WINDOW))
        if size > _SCAN_WINDOW:
            fh.seek(max(0, size - _SCAN_WINDOW))
            tail = fh.read()
        else:
            tail = b""

    for obj in _iter_json_lines(head, drop_first=False):
        if not probe.cwd and obj.get("cwd"):
            probe.cwd = str(obj["cwd"])
        if probe.git_branch is None and obj.get("gitBranch"):
            probe.git_branch = str(obj["gitBranch"])
        if probe.started is None:
            probe.started = _parse_ts(obj.get("timestamp"))
        if not probe.first_prompt and obj.get("type") == "user":
            text = _flatten_content((obj.get("message") or {}).get("content"))
            if text and not text.lstrip().startswith("[{"):
                probe.first_prompt = text
        if obj.get("type") == "ai-title" and obj.get("aiTitle"):
            probe.title = str(obj["aiTitle"])

    for obj in _iter_json_lines(tail, drop_first=True) if tail else []:
        if obj.get("type") == "ai-title" and obj.get("aiTitle"):
            probe.title = str(obj["aiTitle"])
        if obj.get("type") == "last-prompt" and obj.get("lastPrompt"):
            probe.last_prompt = str(obj["lastPrompt"])
        ts = _parse_ts(obj.get("timestamp"))
        if ts and (probe.updated is None or ts > probe.updated):
            probe.updated = ts
        if obj.get("type") == "assistant":
            message = obj.get("message") or {}
            if message.get("model"):
                probe.model = str(message["model"])
            usage = message.get("usage") or {}
            ctx = sum(
                int(usage.get(k, 0) or 0)
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            probe.tokens = max(probe.tokens, ctx)

    probe.user_turns, probe.assistant_turns = _count_turns(path)
    if not probe.title:
        probe.title = (probe.last_prompt or probe.first_prompt or "").strip()
    return probe


def _decode_project_key(key: str) -> Path:
    """`-home-user-projects-foo` -> /home/user/projects/foo (best effort)."""
    return Path("/" + key.lstrip("-").replace("-", "/"))


def _running_sids() -> dict[str, str]:
    """sid -> tmux window name for every claude process currently attached."""
    running: dict[str, str] = {}
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{window_name}\t#{pane_pid}"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    pane_windows = {}
    for line in out.splitlines():
        window, _, pid = line.partition("\t")
        if pid.isdigit():
            pane_windows[int(pid)] = window

    try:
        ps = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return running

    parents: dict[int, int] = {}
    rows: list[tuple[int, str]] = []
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
        parents[pid] = ppid
        rows.append((pid, args))

    for pid, args in rows:
        match = re.search(r"--(?:resume|session-id)[= ]([0-9a-f-]{36})", args)
        if not match or "claude" not in args:
            continue
        sid = match.group(1)
        window = None
        walker, hops = pid, 0
        while walker and hops < 12:
            if walker in pane_windows:
                window = pane_windows[walker]
                break
            walker = parents.get(walker, 0)
            hops += 1
        running[sid] = window or ""
    return running


def _registry() -> dict[str, tuple[Path, LaunchMode]]:
    out: dict[str, tuple[Path, LaunchMode]] = {}
    if not REGISTRY_DIR.is_dir():
        return out
    for rec in REGISTRY_DIR.iterdir():
        if not rec.is_file() or not _SID_RE.match(rec.name):
            continue
        try:
            lines = rec.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        cwd = Path(lines[0]) if lines else Path.home()
        mode = LaunchMode.CLAUDE if len(lines) > 1 and lines[1] == "claude" else LaunchMode.CLAUDEV
        out[rec.name] = (cwd, mode)
    return out


def _load_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("version") != _CACHE_VERSION:
        return {}
    return data.get("entries", {})


def _save_cache(entries: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"version": _CACHE_VERSION, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(CACHE_FILE)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def scan_live() -> list[Session]:
    """Every transcript on disk, newest first."""
    cache = _load_cache()
    fresh: dict[str, dict] = {}
    sessions: list[Session] = []
    if not PROJECTS_DIR.is_dir():
        return sessions

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            sid = path.stem
            if not _SID_RE.match(sid):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            key = str(path)
            cached = cache.get(key)
            if cached and cached.get("size") == st.st_size and cached.get("mtime") == st.st_mtime:
                meta = cached
            else:
                probe = probe_transcript(path)
                meta = {
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "title": probe.title,
                    "last_prompt": probe.last_prompt,
                    "cwd": probe.cwd or str(_decode_project_key(project_dir.name)),
                    "git_branch": probe.git_branch,
                    "model": probe.model,
                    "tokens": probe.tokens,
                    "started": _iso(probe.started),
                    "updated": _iso(probe.updated),
                    "user_turns": probe.user_turns,
                    "assistant_turns": probe.assistant_turns,
                }
            fresh[key] = meta

            updated = (
                datetime.fromisoformat(meta["updated"])
                if meta.get("updated")
                else datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            )
            started = datetime.fromisoformat(meta["started"]) if meta.get("started") else updated
            kind = (
                SessionKind.MATERIALIZED
                if (MATERIALIZED_DIR / f"{sid}.json").exists()
                else SessionKind.LIVE
            )
            sessions.append(
                Session(
                    sid=sid,
                    project=Path(meta["cwd"]),
                    kind=kind,
                    title=meta["title"] or "(без заголовка)",
                    last_prompt=meta["last_prompt"],
                    started=started,
                    updated=updated,
                    prompt_count=meta["user_turns"],
                    message_count=meta["user_turns"] + meta["assistant_turns"],
                    git_branch=meta.get("git_branch"),
                    model=meta.get("model"),
                    tokens=meta.get("tokens", 0),
                    size_bytes=meta["size"],
                    transcript=path,
                )
            )

    _save_cache(fresh)
    sessions.sort(key=lambda s: s.updated or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return sessions


def history_prompts() -> dict[str, list[tuple[datetime, str, str]]]:
    """sid -> [(timestamp, project, prompt)] from ~/.claude/history.jsonl."""
    grouped: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
    if not HISTORY_FILE.is_file():
        return grouped
    with HISTORY_FILE.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = obj.get("sessionId")
            if not isinstance(sid, str) or not _SID_RE.match(sid):
                continue
            ts = _parse_ts(obj.get("timestamp"))
            if ts is None:
                continue
            grouped[sid].append((ts, str(obj.get("project") or ""), str(obj.get("display") or "")))
    for entries in grouped.values():
        entries.sort(key=lambda item: item[0])
    return grouped


def scan_ghosts(live_sids: set[str]) -> list[Session]:
    """Sessions referenced by history.jsonl whose transcript retention deleted."""
    ghosts: list[Session] = []
    for sid, entries in history_prompts().items():
        if sid in live_sids or not entries:
            continue
        project = next((p for _, p, _ in reversed(entries) if p), str(Path.home()))
        prompts = [text for _, _, text in entries if text and not text.startswith("/")]
        title = prompts[0].strip() if prompts else entries[0][2].strip()
        ghosts.append(
            Session(
                sid=sid,
                project=Path(project),
                kind=SessionKind.GHOST,
                title=title or "(только команды)",
                last_prompt=prompts[-1] if prompts else "",
                started=entries[0][0],
                updated=entries[-1][0],
                prompt_count=len(entries),
                message_count=len(entries),
            )
        )
    ghosts.sort(key=lambda s: s.updated or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return ghosts


def build_index() -> list[Session]:
    """Live sessions plus ghosts, annotated with run state and registry mode."""
    live = scan_live()
    live_sids = {s.sid for s in live}
    sessions = live + scan_ghosts(live_sids)

    running = _running_sids()
    registry = _registry()
    for session in sessions:
        if session.sid in running:
            session.run_state = RunState.RUNNING
            session.tmux_window = running[session.sid] or None
        elif session.sid in registry:
            session.run_state = RunState.REGISTERED
        if session.sid in registry:
            session.project, session.launch_mode = registry[session.sid]
    return sessions
