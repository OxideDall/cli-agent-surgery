#!/usr/bin/env python3
"""Deterministic state guard across the compaction boundary.

PreCompact snapshots runtime-owned facts (git, touched files, verbatim user
requests, todos). PostCompact diffs them against the summary it is handed and
queues whatever the summary dropped; UserPromptSubmit delivers that queue once,
on the next turn. Merge-only: the model never writes these facts, so a summary
cannot silently clobber them.

The diff cannot run at SessionStart(compact): measured on 2.1.257, that event
fires 0.06-5.1s before the summary exists (the gap grows with summary size).
PostCompact receives the text directly, so the boundary is race-free.
"""
from __future__ import annotations

import enum
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, TypedDict

STATE = Path(os.environ.get("CLAUDEVS_STATE", Path.home() / ".local/state/claudevs"))
SNAP_DIR = STATE / "snapshots"
AUDIT = STATE / "state-guard.jsonl"
PENDING_DIR = STATE / "pending"

INJECT_BUDGET = 7500          # docs cap additionalContext at 10k chars
MAX_REQUESTS = 12             # verbatim user turns kept per epoch
MAX_REQUEST_CHARS = 400
MAX_FILES = 40
GIT_TIMEOUT = 5
SUMMARY_WAIT = 5.0            # SessionStart(compact) runs ~60ms before the
SUMMARY_POLL = 0.1            # summary line reaches the transcript on disk

WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit", "FileEdit", "FileWrite"})

# Files are just as often created from the shell, which no file_path argument
# records — recover those targets from the command text itself.
MUTATING_SHELL = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:cat\s*>|tee\b|mkdir\b|rm\b|mv\b|cp\b|chmod\b|chown\b|ln\b"
    r"|sed\s+-i|truncate\b|touch\b|install\b"
    r"|git\s+(?:commit|checkout|reset|merge|rebase|apply|restore|add))")
PATH_TOKEN = re.compile(r"(?:~|\.{0,2})/[\w./@+-]+")
SKIP_PATH_PREFIX = ("/dev/", "/proc/", "/sys/")


class HookEvent(str, enum.Enum):
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"


class SessionSource(str, enum.Enum):
    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"
    COMPACT = "compact"
    FORK = "fork"


class FactKind(str, enum.Enum):
    """Ordered by injection priority: the first kinds survive the budget."""
    USER_REQUEST = "user_request"
    TODO = "todo"
    DIRTY_FILE = "dirty_file"
    BRANCH = "branch"
    TOUCHED_FILE = "touched_file"


PRIORITY = tuple(FactKind)

LABEL = {
    FactKind.USER_REQUEST: "Дословные запросы пользователя, потерянные при сжатии",
    FactKind.TODO: "Незакрытые задачи",
    FactKind.DIRTY_FILE: "Изменённые файлы в рабочем дереве",
    FactKind.BRANCH: "Git",
    FactKind.TOUCHED_FILE: "Файлы, правленные в этой сессии",
}


class Fact(TypedDict):
    kind: str
    text: str      # what gets injected
    probe: str     # what must appear in the summary for the fact to count as kept


class Snapshot(TypedDict):
    session_id: str
    at: float
    cwd: str
    transcript: str
    summary_count: int
    facts: list[Fact]


def audit(**row: Any) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    row["at"] = time.strftime("%FT%T%z")
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(("git", "-C", cwd, *args), capture_output=True,
                             text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def transcript_epoch(path: Path) -> Iterator[dict[str, Any]]:
    """Entries since the last compact boundary — the still-uncompacted epoch."""
    epoch: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                    epoch.clear()
                    continue
                epoch.append(rec)
    except OSError:
        return iter(())
    return iter(epoch)


def text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_human_turn(rec: dict[str, Any]) -> bool:
    """Typed prompts only. Slash-command echoes carry no promptSource, and
    task notifications arrive as promptSource=system / origin.kind!=human."""
    origin = rec.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind == "human" or (kind is None and rec.get("promptSource") in ("typed", "sdk"))


def shell_targets(command: str) -> list[str]:
    if not MUTATING_SHELL.search(command):
        return []
    return [t for t in PATH_TOKEN.findall(command)
            if not t.startswith(SKIP_PATH_PREFIX)][:8]


def confirmed_file(token: str, cwd: str, since: float) -> bool:
    path = Path(os.path.expanduser(token))
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        return path.is_file() and path.stat().st_mtime >= since
    except OSError:
        return False


def epoch_started(rec: dict[str, Any], current: float) -> float:
    stamp = rec.get("timestamp")
    if not isinstance(stamp, str):
        return current
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return current


def collect(session_id: str, transcript: str, cwd: str) -> Snapshot:
    requests: deque[str] = deque(maxlen=MAX_REQUESTS)
    legacy: deque[str] = deque(maxlen=MAX_REQUESTS)
    written: dict[str, None] = {}
    shell_seen: dict[str, None] = {}
    todos: list[dict[str, Any]] = []
    since = time.time()
    first = True

    for rec in transcript_epoch(Path(transcript)):
        kind = rec.get("type")
        if first:
            since, first = epoch_started(rec, since), False
        msg = rec.get("message") or {}
        if kind == "user" and not rec.get("isMeta") and not rec.get("isCompactSummary"):
            body = text_blocks(msg.get("content")).strip()
            if not body:
                continue
            if is_human_turn(rec):
                requests.append(body[:MAX_REQUEST_CHARS])
            elif not any(rec.get(f) for f in ("promptSource", "origin")):
                # Pre-2.1 transcripts have neither field: fall back to prose.
                if not body.startswith(("<local-command", "<system-reminder",
                                        "<command-name>", "<task-notification>")):
                    legacy.append(body[:MAX_REQUEST_CHARS])
        elif kind == "assistant":
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, args = block.get("name"), block.get("input") or {}
                if name in WRITE_TOOLS and args.get("file_path"):
                    written[str(args["file_path"])] = None
                elif name == "Bash" and isinstance(args.get("command"), str):
                    for target in shell_targets(args["command"]):
                        shell_seen.setdefault(target, None)
                elif name == "TodoWrite" and isinstance(args.get("todos"), list):
                    todos = args["todos"]

    facts: list[Fact] = []
    seen: set[str] = set()
    for body in (requests or legacy):
        if body in seen:
            continue
        seen.add(body)
        facts.append(Fact(kind=FactKind.USER_REQUEST.value, text=body, probe=body[:60]))

    for token in shell_seen:
        if confirmed_file(token, cwd, since):
            written.setdefault(token, None)

    for todo in todos:
        if not isinstance(todo, dict) or todo.get("status") == "completed":
            continue
        label = str(todo.get("content") or todo.get("activeForm") or "").strip()
        if label:
            facts.append(Fact(kind=FactKind.TODO.value,
                              text=f"[{todo.get('status', '?')}] {label}", probe=label[:40]))

    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        head = git(cwd, "rev-parse", "--short", "HEAD")
        facts.append(Fact(kind=FactKind.BRANCH.value,
                          text=f"ветка {branch} @ {head}, cwd {cwd}", probe=branch))
        for line in git(cwd, "status", "--porcelain").splitlines()[:MAX_FILES]:
            path = line[3:].strip()
            if path:
                facts.append(Fact(kind=FactKind.DIRTY_FILE.value,
                                  text=f"{line[:2].strip() or '??'} {path}",
                                  probe=os.path.basename(path)))

    for path in list(written)[:MAX_FILES]:
        facts.append(Fact(kind=FactKind.TOUCHED_FILE.value, text=path,
                          probe=os.path.basename(path)))

    return Snapshot(session_id=session_id, at=time.time(), cwd=cwd, transcript=transcript,
                    summary_count=scan_summaries(transcript)[0], facts=facts)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold()


def scan_summaries(transcript: str) -> tuple[int, str]:
    """(count, newest text) of compaction summaries — the count tells a fresh
    summary from the one left by an earlier boundary."""
    count, found = 0, ""
    try:
        with Path(transcript).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"isCompactSummary":true' not in line.replace(" ", ""):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                body = text_blocks((rec.get("message") or {}).get("content"))
                if body:
                    count += 1
                    found = body
    except OSError:
        return 0, ""
    return count, found


def await_summary(transcript: str, seen: int) -> str:
    """Wait for a summary newer than the one present at PreCompact."""
    deadline = time.monotonic() + SUMMARY_WAIT
    while True:
        count, body = scan_summaries(transcript)
        if count > seen:
            return body
        if time.monotonic() >= deadline:
            return ""
        time.sleep(SUMMARY_POLL)


def latest_summary(transcript: str) -> str:
    """Newest compaction summary in the transcript; independent of hook order."""
    found = ""
    try:
        with Path(transcript).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"isCompactSummary":true' not in line.replace(" ", ""):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                body = text_blocks((rec.get("message") or {}).get("content"))
                if body:
                    found = body
    except OSError:
        return ""
    return found


def build_injection(missing: list[Fact]) -> str:
    lines = ["Проверка состояния после сжатия (claudevs): факты ниже собраны рантаймом "
             "до сжатия и отсутствуют в сводке. Считать их актуальными."]
    used = len(lines[0])
    for kind in PRIORITY:
        group = [f for f in missing if f["kind"] == kind.value]
        if not group:
            continue
        header = f"\n## {LABEL[kind]}"
        if used + len(header) > INJECT_BUDGET:
            break
        lines.append(header)
        used += len(header)
        for fact in group:
            entry = f"\n- {fact['text']}"
            if used + len(entry) > INJECT_BUDGET:
                lines.append("\n- …обрезано по бюджету")
                return "".join(lines)
            lines.append(entry)
            used += len(entry)
    return "".join(lines)


def on_pre_compact(payload: dict[str, Any]) -> None:
    sid = payload.get("session_id") or "unknown"
    snap = collect(sid, payload.get("transcript_path") or "", payload.get("cwd") or os.getcwd())
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{sid}.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    audit(event=HookEvent.PRE_COMPACT.value, session_id=sid,
          trigger=payload.get("trigger"), facts=len(snap["facts"]))


def on_session_start_unused(payload: dict[str, Any]) -> None:
    sid = payload.get("session_id") or "unknown"
    snap_file = SNAP_DIR / f"{sid}.json"
    if not snap_file.is_file():
        audit(event=HookEvent.SESSION_START.value, session_id=sid, verdict="no_snapshot")
        return
    try:
        snap: Snapshot = json.loads(snap_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        audit(event=HookEvent.SESSION_START.value, session_id=sid, verdict="snapshot_unreadable")
        return

    transcript = payload.get("transcript_path") or snap.get("transcript") or ""
    summary = await_summary(transcript, snap.get("summary_count", 0))
    snap_file.rename(SNAP_DIR / f"{sid}.{int(time.time())}.done.json")

    if summary:
        hay = normalize(summary)
        missing = [f for f in snap["facts"] if normalize(f["probe"]) not in hay]
        verdict = "checked"
    else:
        # Compaction happened but its summary never landed in reach. Re-assert
        # only the facts whose loss hurts most, rather than guess what survived.
        keep = (FactKind.USER_REQUEST.value, FactKind.TODO.value)
        missing = [f for f in snap["facts"] if f["kind"] in keep]
        verdict = "summary_timeout"

    audit(event=HookEvent.SESSION_START.value, session_id=sid, verdict=verdict,
          facts=len(snap["facts"]), missing=len(missing), summary_chars=len(summary))
    if not missing:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": HookEvent.SESSION_START.value,
        "additionalContext": build_injection(missing),
    }}, ensure_ascii=False))


def on_post_compact(payload: dict[str, Any]) -> None:
    sid = payload.get("session_id") or "unknown"
    summary = payload.get("compact_summary") or ""
    snap_file = SNAP_DIR / f"{sid}.json"
    if not snap_file.is_file():
        audit(event=HookEvent.POST_COMPACT.value, session_id=sid, verdict="no_snapshot",
              summary_chars=len(summary))
        return
    try:
        snap: Snapshot = json.loads(snap_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        audit(event=HookEvent.POST_COMPACT.value, session_id=sid, verdict="snapshot_unreadable")
        return
    snap_file.rename(SNAP_DIR / f"{sid}.{int(time.time())}.done.json")

    hay = normalize(summary)
    missing = [f for f in snap["facts"] if normalize(f["probe"]) not in hay]
    audit(event=HookEvent.POST_COMPACT.value, session_id=sid, verdict="checked",
          trigger=payload.get("trigger"), facts=len(snap["facts"]),
          missing=len(missing), summary_chars=len(summary))
    if not missing:
        return
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    (PENDING_DIR / f"{sid}.md").write_text(build_injection(missing), encoding="utf-8")


def on_user_prompt_submit(payload: dict[str, Any]) -> None:
    """Deliver the queued facts once, on the first turn after a compaction."""
    sid = payload.get("session_id") or "unknown"
    pending = PENDING_DIR / f"{sid}.md"
    if not pending.is_file():
        return
    try:
        body = pending.read_text(encoding="utf-8")
    finally:
        pending.unlink(missing_ok=True)
    audit(event=HookEvent.USER_PROMPT_SUBMIT.value, session_id=sid,
          verdict="delivered", chars=len(body))
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": HookEvent.USER_PROMPT_SUBMIT.value,
        "additionalContext": body,
    }}, ensure_ascii=False))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        audit(event="?", verdict="unparsed_stdin")
        return 0
    event = payload.get("hook_event_name")
    try:
        if event == HookEvent.PRE_COMPACT.value:
            on_pre_compact(payload)
        elif event == HookEvent.POST_COMPACT.value:
            on_post_compact(payload)
        elif event == HookEvent.USER_PROMPT_SUBMIT.value:
            on_user_prompt_submit(payload)
    except Exception as exc:                      # a hook must never break a session
        audit(event=event, verdict="error", error=f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
