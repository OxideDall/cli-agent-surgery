"""Rebuilds lost sessions from surviving artifacts: prompts + git history.

Retention deleted the transcripts, but the prompts live in history.jsonl and the
work itself lives in the project's git history. Correlating the two by timestamp
yields an evidence-backed reconstruction — every claim traces to a commit.
"""

from __future__ import annotations

import enum
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .models import Session
from .scanner import history_prompts

GRACE_AFTER = timedelta(hours=12)
GRACE_BEFORE = timedelta(hours=1)
_MAX_DIFF_BYTES = 60_000


class EvidenceKind(str, enum.Enum):
    """Where a piece of reconstruction evidence came from."""

    PROMPT = "prompt"
    COMMIT = "commit"
    FILE_MTIME = "file_mtime"
    DNF = "dnf"


class Confidence(str, enum.Enum):
    """How well the surviving artifacts cover a session."""

    STRONG = "strong"
    """Prompts plus in-window commits touching the project."""

    PARTIAL = "partial"
    """Prompts plus file mtimes or system traces, no commits."""

    PROMPTS_ONLY = "prompts_only"
    """Nothing but the prompt log survived."""


@dataclass(slots=True)
class Commit:
    sha: str
    when: datetime
    subject: str
    files: tuple[str, ...]
    insertions: int
    deletions: int


@dataclass(slots=True)
class Reconstruction:
    """Everything recoverable about one lost session."""

    session: Session
    prompts: list[tuple[datetime, str]] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    confidence: Confidence = Confidence.PROMPTS_ONLY

    @property
    def churn(self) -> tuple[int, int]:
        return (
            sum(c.insertions for c in self.commits),
            sum(c.deletions for c in self.commits),
        )


def _git(repo: Path, *args: str, timeout: int = 20) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def commits_in_window(repo: Path, start: datetime, end: datetime) -> list[Commit]:
    """Commits on any ref authored inside the padded session window."""
    since = (start - GRACE_BEFORE).strftime("%Y-%m-%dT%H:%M:%S")
    until = (end + GRACE_AFTER).strftime("%Y-%m-%dT%H:%M:%S")
    raw = _git(
        repo, "log", "--all", "--since", since, "--until", until,
        "--numstat", "--date=iso-strict",
        "--pretty=format:%x00%H%x1f%aI%x1f%s",
    )
    commits: list[Commit] = []
    for block in raw.split("\x00"):
        block = block.strip("\n")
        if not block:
            continue
        header, _, body = block.partition("\n")
        parts = header.split("\x1f")
        if len(parts) < 3:
            continue
        sha, iso, subject = parts[0], parts[1], parts[2]
        try:
            when = datetime.fromisoformat(iso)
        except ValueError:
            continue
        files: list[str] = []
        ins = dele = 0
        for line in body.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            add, rem, name = cols
            files.append(name)
            ins += int(add) if add.isdigit() else 0
            dele += int(rem) if rem.isdigit() else 0
        commits.append(Commit(sha, when, subject, tuple(files), ins, dele))
    commits.sort(key=lambda c: c.when)
    return commits


def commit_diff(repo: Path, sha: str) -> str:
    """Truncated patch for one commit — enough to judge intent, bounded in size."""
    patch = _git(repo, "show", "--stat", "--patch", "--no-color", sha, timeout=30)
    if len(patch) > _MAX_DIFF_BYTES:
        return patch[:_MAX_DIFF_BYTES] + "\n… (дифф усечён)\n"
    return patch


def reconstruct(session: Session) -> Reconstruction:
    """Gather prompts and git evidence for one ghost session."""
    entries = history_prompts().get(session.sid, [])
    prompts = [(ts, text) for ts, _, text in entries if text]
    result = Reconstruction(session=session, prompts=prompts)

    start = session.started or (prompts[0][0] if prompts else None)
    end = session.updated or (prompts[-1][0] if prompts else None)
    if start and end and is_repo(session.project):
        result.commits = commits_in_window(session.project, start, end)
        result.touched_files = sorted({f for c in result.commits for f in c.files})

    if result.commits:
        result.confidence = Confidence.STRONG
    elif result.touched_files:
        result.confidence = Confidence.PARTIAL
    return result


def render_brief(result: Reconstruction) -> str:
    """Markdown dossier handed to Claude for narrative reconstruction."""
    session = result.session
    lines = [
        f"# Реконструкция сессии {session.sid}",
        "",
        f"- Проект: `{session.project}`",
        f"- Окно: {session.started:%Y-%m-%d %H:%M} — {session.updated:%Y-%m-%d %H:%M}",
        f"- Уверенность: **{result.confidence.value}**",
        f"- Промптов: {len(result.prompts)}; коммитов в окне: {len(result.commits)}",
        "",
        "Транскрипт удалён retention-свипом Claude Code. Ответы ассистента и "
        "вызовы инструментов утрачены безвозвратно. Ниже — только уцелевшие "
        "артефакты. Не додумывай того, чего в них нет.",
        "",
        "## Промпты пользователя (хронологически)",
        "",
    ]
    for ts, text in result.prompts:
        snippet = text.strip().replace("\n", "\n      ")
        lines.append(f"- `{ts:%m-%d %H:%M}` {snippet}")

    if result.commits:
        ins, dele = result.churn
        lines += [
            "",
            f"## Коммиты в окне сессии (+{ins}/-{dele} строк)",
            "",
        ]
        for c in result.commits:
            lines.append(
                f"- `{c.sha[:10]}` `{c.when:%m-%d %H:%M}` {c.subject} "
                f"— {len(c.files)} файл(ов), +{c.insertions}/-{c.deletions}"
            )
        lines += ["", "## Затронутые файлы", ""]
        lines += [f"- `{f}`" for f in result.touched_files[:80]]
        if len(result.touched_files) > 80:
            lines.append(f"- … ещё {len(result.touched_files) - 80}")
    else:
        lines += [
            "",
            "## Коммиты",
            "",
            "Git-доказательств в окне нет — проект не репозиторий либо работа не "
            "закоммичена. Реконструкция ограничена намерением из промптов.",
        ]

    lines += [
        "",
        "## Задача",
        "",
        "Восстанови, что происходило в сессии: какая была цель, что реально "
        "сделано, что осталось незакрытым. Каждое утверждение о выполненной "
        "работе привязывай к конкретному коммиту (`sha`). Там, где промпт не "
        "подкреплён коммитом, помечай это явно как «намерение без подтверждения».",
    ]
    return "\n".join(lines)
