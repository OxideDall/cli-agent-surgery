"""Turns a ghost session into a transcript Claude Code can actually resume.

The rebuilt transcript alternates user/assistant turns so the API sees a valid
conversation; every lost assistant turn is an explicit placeholder rather than
invented text, and git evidence is attached as the opening turn when available.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Session, SessionKind
from .reconstruct import Confidence, Reconstruction, reconstruct, render_brief
from .scanner import MATERIALIZED_DIR, PROJECTS_DIR

_VERSION = "claudev-materialized/1"
_LOST = "⟪ответ ассистента утрачен: транскрипт удалён retention-свипом Claude Code⟫"


class MaterializeError(RuntimeError):
    """Raised when a ghost cannot be rebuilt."""


def project_key(cwd: Path) -> str:
    """Encode a cwd the way Claude Code names its project directories."""
    return str(cwd).replace("/", "-").replace(".", "-").replace("_", "-")


def _base(sid: str, cwd: Path, ts: datetime) -> dict:
    return {
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": str(cwd),
        "sessionId": sid,
        "version": _VERSION,
        "gitBranch": "",
        "timestamp": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _user_entry(sid: str, cwd: Path, ts: datetime, text: str, parent: str | None) -> dict:
    return {
        **_base(sid, cwd, ts),
        "parentUuid": parent,
        "type": "user",
        "promptId": str(uuid.uuid4()),
        "uuid": str(uuid.uuid4()),
        "message": {"role": "user", "content": text},
        "permissionMode": "bypassPermissions",
        "origin": {"kind": "human"},
        "promptSource": "typed",
    }


def _assistant_entry(sid: str, cwd: Path, ts: datetime, text: str, parent: str) -> dict:
    return {
        **_base(sid, cwd, ts),
        "parentUuid": parent,
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "requestId": f"reconstructed_{uuid.uuid4().hex[:16]}",
        "message": {
            "id": f"msg_reconstructed_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": "reconstructed",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        },
    }


def _preamble(result: Reconstruction) -> str:
    session = result.session
    ins, dele = result.churn
    header = [
        "Это ВОССТАНОВЛЕННАЯ сессия. Оригинальный транскрипт был удалён "
        "автоматическим retention-свипом Claude Code (`cleanupPeriodDays`, "
        "дефолт 30 дней). Уцелели только мои промпты из `~/.claude/history.jsonl`; "
        "твои ответы и все вызовы инструментов утрачены.",
        "",
        f"Проект: `{session.project}`",
        f"Период: {session.started:%Y-%m-%d %H:%M} — {session.updated:%Y-%m-%d %H:%M}",
        f"Промптов восстановлено: {len(result.prompts)}",
    ]
    if result.confidence is Confidence.STRONG:
        header += [
            f"Git-доказательства: {len(result.commits)} коммитов, "
            f"{len(result.touched_files)} файлов, +{ins}/-{dele} строк.",
            "",
            "Ниже идут мои реальные промпты в хронологическом порядке. На месте "
            "каждого твоего ответа стоит заглушка. Сводка git-активности за тот же "
            "период приложена — сверься с ней и с текущим состоянием проекта, "
            "прежде чем что-то утверждать о сделанном.",
            "",
            "---",
            "",
            render_brief(result),
        ]
    else:
        header += [
            "",
            "Git-доказательств нет: проект не репозиторий либо работа не "
            "закоммичена. Доступно только намерение из промптов.",
        ]
    return "\n".join(header)


def materialize(session: Session, *, force: bool = False) -> Path:
    """Write a resumable transcript for a ghost; returns its path."""
    if session.kind is not SessionKind.GHOST:
        raise MaterializeError(f"{session.sid}: не призрак, материализация не нужна")

    result = reconstruct(session)
    if not result.prompts:
        raise MaterializeError(f"{session.sid}: не осталось ни одного промпта")

    cwd = session.project if session.project.is_dir() else Path.home()
    target_dir = PROJECTS_DIR / project_key(cwd)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{session.sid}.jsonl"
    if target.exists() and not force:
        raise MaterializeError(f"{target} уже существует")

    first_ts = result.prompts[0][0]
    entries: list[dict] = []
    parent: str | None = None

    opener = _user_entry(session.sid, cwd, first_ts - timedelta(seconds=2), _preamble(result), None)
    entries.append(opener)
    parent = opener["uuid"]
    ack = _assistant_entry(
        session.sid, cwd, first_ts - timedelta(seconds=1),
        "Принято. Ниже — восстановленный лог промптов; мои прежние ответы "
        "недоступны, буду опираться на промпты, git-историю и текущее состояние "
        "файлов.", parent,
    )
    entries.append(ack)
    parent = ack["uuid"]

    for ts, text in result.prompts:
        user = _user_entry(session.sid, cwd, ts, text, parent)
        entries.append(user)
        parent = user["uuid"]
        assistant = _assistant_entry(session.sid, cwd, ts + timedelta(milliseconds=1), _LOST, parent)
        entries.append(assistant)
        parent = assistant["uuid"]

    entries.append(
        {"type": "ai-title", "aiTitle": f"[восстановлено] {session.title[:70]}",
         "sessionId": session.sid}
    )
    entries.append(
        {"type": "last-prompt", "lastPrompt": result.prompts[-1][1],
         "leafUuid": parent, "sessionId": session.sid}
    )

    tmp = target.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, target)

    MATERIALIZED_DIR.mkdir(parents=True, exist_ok=True)
    (MATERIALIZED_DIR / f"{session.sid}.json").write_text(
        json.dumps(
            {
                "sid": session.sid,
                "project": str(cwd),
                "confidence": result.confidence.value,
                "prompts": len(result.prompts),
                "commits": [c.sha for c in result.commits],
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "transcript": str(target),
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return target


def materialize_all(sessions: list[Session], *, only_strong: bool = False) -> dict[str, str]:
    """Bulk-rebuild ghosts; returns sid -> path or error message."""
    outcome: dict[str, str] = {}
    for session in sessions:
        if session.kind is not SessionKind.GHOST:
            continue
        if only_strong and reconstruct(session).confidence is not Confidence.STRONG:
            continue
        try:
            outcome[session.sid] = str(materialize(session))
        except (MaterializeError, OSError) as exc:
            outcome[session.sid] = f"ошибка: {exc}"
    return outcome
