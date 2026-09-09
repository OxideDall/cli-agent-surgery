"""Domain types for the claudev session index. All closed sets are Enums."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TypedDict


class SessionKind(str, enum.Enum):
    """Whether a session still has a transcript behind it."""

    LIVE = "live"
    """Transcript .jsonl present — fully resumable."""

    GHOST = "ghost"
    """Transcript deleted by retention; only user prompts survive in history.jsonl."""

    MATERIALIZED = "materialized"
    """Ghost rebuilt into a resumable transcript containing prompts only."""


class RunState(str, enum.Enum):
    """Liveness of the session's claude process right now."""

    RUNNING = "running"
    """A claude process is attached to this session id."""

    REGISTERED = "registered"
    """In the claudev registry (survived a reboot) but no process yet."""

    IDLE = "idle"


class LaunchMode(str, enum.Enum):
    """Which binary the session was originally started with."""

    CLAUDEV = "claudev"
    CLAUDE = "claude"


class SortKey(str, enum.Enum):
    """User-selectable ordering of the session list."""

    RECENT = "recent"
    OLDEST = "oldest"
    PROJECT = "project"
    SIZE = "size"
    PROMPTS = "prompts"
    TITLE = "title"


class Scope(str, enum.Enum):
    """Which subset of the index a view shows."""

    FAVORITES = "favorites"
    ALL = "all"
    RUNNING = "running"
    ARCHIVE = "archive"


class EntryRole(str, enum.Enum):
    """Speaker of a rendered transcript entry."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class FavoriteRecord(TypedDict):
    """One favorited session as persisted to JSON."""

    sid: str
    note: str
    tags: list[str]
    pinned_at: float


class StoreFile(TypedDict):
    """On-disk shape of the favorites/state file."""

    version: int
    favorites: list[FavoriteRecord]
    last_opened: dict[str, float]


@dataclass(slots=True)
class TranscriptEntry:
    """One renderable turn extracted from a transcript."""

    role: EntryRole
    text: str
    timestamp: datetime | None = None
    tool_name: str | None = None


@dataclass(slots=True)
class Session:
    """A single Claude session, live or reconstructed."""

    sid: str
    project: Path
    kind: SessionKind
    title: str
    last_prompt: str = ""
    started: datetime | None = None
    updated: datetime | None = None
    prompt_count: int = 0
    message_count: int = 0
    git_branch: str | None = None
    model: str | None = None
    tokens: int = 0
    size_bytes: int = 0
    transcript: Path | None = None
    run_state: RunState = RunState.IDLE
    launch_mode: LaunchMode = LaunchMode.CLAUDEV
    tmux_window: str | None = None
    favorite: bool = False
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def resumable(self) -> bool:
        return self.kind is not SessionKind.GHOST

    @property
    def project_label(self) -> str:
        """Short project name — last two path segments, `~` for home."""
        home = Path.home()
        if self.project == home:
            return "~"
        try:
            rel = self.project.relative_to(home)
        except ValueError:
            return str(self.project)
        parts = rel.parts
        return "/".join(parts[-2:]) if len(parts) > 1 else rel.name
