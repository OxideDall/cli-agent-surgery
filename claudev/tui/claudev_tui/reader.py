"""Reads renderable turns out of the tail of a transcript."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import EntryRole, Session, SessionKind, TranscriptEntry
from .reconstruct import Confidence, reconstruct
from .scanner import _flatten_content, _iter_json_lines, _parse_ts

_TAIL_BYTES = 512 * 1024
_MAX_TEXT = 1800


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + " …"


def tail_entries(path: Path, limit: int = 40) -> list[TranscriptEntry]:
    """Last `limit` conversational turns, oldest first."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                blob = fh.read()
                partial = True
            else:
                blob = fh.read()
                partial = False
    except OSError:
        return []

    entries: list[TranscriptEntry] = []
    for obj in _iter_json_lines(blob, drop_first=partial):
        kind = obj.get("type")
        ts = _parse_ts(obj.get("timestamp"))
        message = obj.get("message") or {}
        if kind == "user":
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                names = [
                    str(b.get("tool_use_id", ""))[:14]
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                entries.append(
                    TranscriptEntry(EntryRole.TOOL, f"результат инструмента ({', '.join(names)})", ts)
                )
                continue
            text = _flatten_content(content)
            if text:
                entries.append(TranscriptEntry(EntryRole.USER, _clip(text), ts))
        elif kind == "assistant":
            content = message.get("content")
            text = _flatten_content(content)
            tools = [
                str(b.get("name", "?"))
                for b in (content if isinstance(content, list) else [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if text:
                entries.append(TranscriptEntry(EntryRole.ASSISTANT, _clip(text), ts))
            for name in tools:
                entries.append(TranscriptEntry(EntryRole.TOOL, f"→ {name}", ts, tool_name=name))
        elif kind == "system" and obj.get("subtype"):
            entries.append(
                TranscriptEntry(EntryRole.SYSTEM, f"[{obj['subtype']}]", ts)
            )
    return entries[-limit:]


def _fmt_ts(ts: datetime | None) -> str:
    return f"{ts:%m-%d %H:%M}" if ts else "  --  "


def preview_markup(session: Session) -> str:
    """Rich markup preview for the detail pane."""
    head = [
        f"[b]{_esc(session.title)}[/b]",
        f"[dim]{_esc(str(session.project))}[/dim]",
        "",
    ]
    meta = [f"[cyan]sid[/] {session.sid}"]
    if session.started and session.updated:
        meta.append(f"[cyan]период[/] {session.started:%Y-%m-%d %H:%M} → {session.updated:%Y-%m-%d %H:%M}")
    meta.append(f"[cyan]промптов[/] {session.prompt_count}   [cyan]сообщений[/] {session.message_count}")
    if session.model:
        meta.append(f"[cyan]модель[/] {_esc(session.model)}")
    if session.tokens:
        meta.append(f"[cyan]контекст[/] ~{session.tokens // 1000}k токенов")
    if session.git_branch:
        meta.append(f"[cyan]ветка[/] {_esc(session.git_branch)}")
    if session.size_bytes:
        meta.append(f"[cyan]размер[/] {session.size_bytes / 1024:.0f} КБ")
    if session.note:
        meta.append(f"[yellow]заметка[/] {_esc(session.note)}")
    if session.tags:
        meta.append("[yellow]теги[/] " + " ".join(f"#{_esc(t)}" for t in session.tags))
    head += meta + ["", "[dim]" + "─" * 56 + "[/dim]", ""]

    if session.kind is SessionKind.GHOST:
        return "\n".join(head + _ghost_body(session))

    if session.transcript is None:
        return "\n".join(head + ["[dim]транскрипт недоступен[/dim]"])

    body: list[str] = []
    for entry in tail_entries(session.transcript):
        stamp = f"[dim]{_fmt_ts(entry.timestamp)}[/dim]"
        match entry.role:
            case EntryRole.USER:
                body += [f"{stamp} [b green]▸ ты[/b green]", _esc(entry.text), ""]
            case EntryRole.ASSISTANT:
                body += [f"{stamp} [b magenta]◂ claude[/b magenta]", _esc(entry.text), ""]
            case EntryRole.TOOL:
                body.append(f"{stamp} [dim blue]{_esc(entry.text)}[/dim blue]")
            case EntryRole.SYSTEM:
                body.append(f"{stamp} [dim]{_esc(entry.text)}[/dim]")
    if not body:
        body = ["[dim]нечего показать[/dim]"]
    return "\n".join(head + body)


def _ghost_body(session: Session) -> list[str]:
    result = reconstruct(session)
    colour = {
        Confidence.STRONG: "green",
        Confidence.PARTIAL: "yellow",
        Confidence.PROMPTS_ONLY: "red",
    }[result.confidence]
    lines = [
        f"[b {colour}]ПРИЗРАК[/] — транскрипт удалён retention-свипом.",
        f"Уверенность реконструкции: [{colour}]{result.confidence.value}[/{colour}]",
    ]
    if result.commits:
        ins, dele = result.churn
        lines += [
            f"Git-доказательства: [b]{len(result.commits)}[/b] коммитов, "
            f"[b]{len(result.touched_files)}[/b] файлов, [green]+{ins}[/green]/[red]-{dele}[/red]",
            "",
            "[b]Коммиты в окне сессии[/b]",
        ]
        for commit in result.commits[:12]:
            lines.append(
                f"  [dim]{commit.when:%m-%d %H:%M}[/dim] [yellow]{commit.sha[:8]}[/yellow] "
                f"{_esc(commit.subject[:64])}"
            )
        if len(result.commits) > 12:
            lines.append(f"  [dim]… ещё {len(result.commits) - 12}[/dim]")
    else:
        lines.append("[dim]Git-доказательств нет — только промпты.[/dim]")

    lines += ["", "[b]Промпты[/b]", ""]
    for ts, text in result.prompts[:40]:
        lines += [f"[dim]{_fmt_ts(ts)}[/dim] [b green]▸[/b green] {_esc(_clip(text))}", ""]
    if len(result.prompts) > 40:
        lines.append(f"[dim]… ещё {len(result.prompts) - 40} промптов[/dim]")
    lines += ["", "[dim]Нажми [b]m[/b] чтобы материализовать в резюмируемую сессию.[/dim]"]
    return lines


def _esc(text: str) -> str:
    """Rich markup treats square brackets as tags."""
    return text.replace("[", "\\[")
