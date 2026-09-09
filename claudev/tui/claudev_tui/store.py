"""Favorites, notes and tags — claudev's own state, never written into ~/.claude."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import FavoriteRecord, Session, StoreFile

STATE_DIR = Path.home() / ".local" / "state" / "claudev"
STORE_FILE = STATE_DIR / "favorites.json"
_VERSION = 1


class FavoriteStore:
    """Durable set of pinned sessions with per-session note and tags."""

    def __init__(self, path: Path = STORE_FILE) -> None:
        self._path = path
        self._data: StoreFile = {"version": _VERSION, "favorites": [], "last_opened": {}}
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return
        favorites = [f for f in raw.get("favorites", []) if isinstance(f, dict) and f.get("sid")]
        last_opened = {
            k: float(v)
            for k, v in (raw.get("last_opened") or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
        self._data = {"version": _VERSION, "favorites": favorites, "last_opened": last_opened}

    def _flush(self) -> None:
        """Atomic write — a torn favorites file would silently lose pins."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _find(self, sid: str) -> FavoriteRecord | None:
        return next((f for f in self._data["favorites"] if f["sid"] == sid), None)

    def is_favorite(self, sid: str) -> bool:
        return self._find(sid) is not None

    def toggle(self, sid: str) -> bool:
        """Returns the new favorite state."""
        record = self._find(sid)
        if record is not None:
            self._data["favorites"].remove(record)
            self._flush()
            return False
        self._data["favorites"].append(
            FavoriteRecord(sid=sid, note="", tags=[], pinned_at=time.time())
        )
        self._flush()
        return True

    def set_note(self, sid: str, note: str) -> None:
        record = self._find(sid)
        if record is None:
            self.toggle(sid)
            record = self._find(sid)
        if record is not None:
            record["note"] = note
            self._flush()

    def set_tags(self, sid: str, tags: list[str]) -> None:
        record = self._find(sid)
        if record is None:
            self.toggle(sid)
            record = self._find(sid)
        if record is not None:
            record["tags"] = sorted({t.strip() for t in tags if t.strip()})
            self._flush()

    def mark_opened(self, sid: str) -> None:
        self._data["last_opened"][sid] = time.time()
        self._flush()

    def last_opened(self, sid: str) -> float:
        return self._data["last_opened"].get(sid, 0.0)

    def all_tags(self) -> list[str]:
        return sorted({t for f in self._data["favorites"] for t in f.get("tags", [])})

    def annotate(self, sessions: list[Session]) -> None:
        """Stamp favorite/note/tags onto freshly scanned sessions."""
        by_sid = {f["sid"]: f for f in self._data["favorites"]}
        for session in sessions:
            record = by_sid.get(session.sid)
            if record is None:
                continue
            session.favorite = True
            session.note = record.get("note", "")
            session.tags = tuple(record.get("tags", []))
