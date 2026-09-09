"""Textual picker over every Claude session — live, running and reconstructed."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static, Tab, Tabs

from .launcher import LaunchError, copy_to_clipboard, focus_existing, kill_session, open_window, resume_command
from .materialize import MaterializeError, materialize
from .models import RunState, Scope, Session, SessionKind, SortKey
from .reader import preview_markup
from .scanner import build_index
from .store import FavoriteStore

_SCOPE_TABS: tuple[tuple[Scope, str], ...] = (
    (Scope.FAVORITES, "★ Избранное"),
    (Scope.ALL, "Все"),
    (Scope.RUNNING, "Запущенные"),
    (Scope.ARCHIVE, "Архив"),
)

_SORT_LABELS: dict[SortKey, str] = {
    SortKey.RECENT: "недавние",
    SortKey.OLDEST: "старые",
    SortKey.PROJECT: "проект",
    SortKey.SIZE: "размер",
    SortKey.PROMPTS: "промпты",
    SortKey.TITLE: "заголовок",
}

_KIND_MARK: dict[SessionKind, str] = {
    SessionKind.LIVE: "[green]●[/green]",
    SessionKind.MATERIALIZED: "[cyan]◆[/cyan]",
    SessionKind.GHOST: "[red dim]○[/red dim]",
}

_STATE_MARK: dict[RunState, str] = {
    RunState.RUNNING: "[b green]▶[/b green]",
    RunState.REGISTERED: "[yellow]‖[/yellow]",
    RunState.IDLE: " ",
}


def _ago(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    delta = datetime.now(tz=timezone.utc) - moment.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{max(seconds // 60, 0)}м"
    if seconds < 86400:
        return f"{seconds // 3600}ч"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}д"
    return f"{seconds // (86400 * 30)}мес"


class TextPrompt(ModalScreen[str | None]):
    """One-line modal input used for notes and tags."""

    BINDINGS = [Binding("escape", "dismiss_none", "Отмена")]

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self._title = title
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title, id="prompt-title")
            yield Input(value=self._initial, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ClaudevPicker(App[Session | None]):
    """Session browser: search, favorite, preview, resume, reconstruct."""

    CSS = """
    Screen { layers: base overlay; }
    #body { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 4fr; border-left: solid $panel; padding: 0 1; }
    #search { border: none; height: 3; background: $boost; }
    #status { height: 1; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    DataTable > .datatable--cursor { background: $accent 40%; }
    #prompt-box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: thick $accent;
        margin: 4 8;
    }
    TextPrompt { align: center middle; }
    """

    BINDINGS = [
        Binding("enter", "open", "Открыть", priority=True),
        Binding("f,space", "favorite", "★ избранное"),
        Binding("m", "materialize", "Материализовать"),
        Binding("n", "note", "Заметка"),
        Binding("t", "tags", "Теги"),
        Binding("slash", "search", "Поиск"),
        Binding("s", "sort", "Сортировка"),
        Binding("r", "refresh", "Пересканировать"),
        Binding("c", "copy", "Копировать cmd"),
        Binding("x", "kill", "Убить сессию"),
        Binding("escape", "clear", "Сброс", show=False),
        Binding("1", "scope('favorites')", "", show=False),
        Binding("2", "scope('all')", "", show=False),
        Binding("3", "scope('running')", "", show=False),
        Binding("4", "scope('archive')", "", show=False),
        Binding("q,ctrl+c", "quit", "Выход"),
    ]

    def __init__(self, select_to: Path | None = None) -> None:
        super().__init__()
        # When set, choosing a session writes its id here and exits instead of
        # spawning a viewer — lets the caller resume in the terminal it owns.
        self.select_to = select_to
        self.store = FavoriteStore()
        self.sessions: list[Session] = []
        self.view: list[Session] = []
        self.scope = Scope.FAVORITES
        self.sort = SortKey.RECENT
        self.query = ""
        self._ui_ready = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Tabs(*(Tab(label, id=scope.value) for scope, label in _SCOPE_TABS))
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(placeholder="поиск: заголовок, проект, промпт, #тег", id="search")
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="right"):
                yield Static("", id="detail", markup=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column("", key="kind", width=2)
        table.add_column("", key="run", width=2)
        table.add_column("★", key="fav", width=2)
        table.add_column("когда", key="when", width=6)
        table.add_column("проект", key="project", width=22)
        table.add_column("заголовок", key="title")
        table.add_column("промпт.", key="prompts", width=7)
        table.focus()
        self._ui_ready = True
        self.refresh_index()

    @work(thread=True, exclusive=True)
    def refresh_index(self) -> None:
        sessions = build_index()
        self.store.reload()
        self.store.annotate(sessions)
        self.app.call_from_thread(self._apply_index, sessions)

    def _apply_index(self, sessions: list[Session]) -> None:
        self.sessions = sessions
        # Start on Favorites only when there is something pinned.
        if not any(s.favorite for s in sessions) and self.scope is Scope.FAVORITES:
            self.scope = Scope.ALL
            self.query_one(Tabs).active = Scope.ALL.value
        self.rebuild()

    def _in_scope(self, session: Session) -> bool:
        match self.scope:
            case Scope.FAVORITES:
                return session.favorite
            case Scope.RUNNING:
                return session.run_state is not RunState.IDLE
            case Scope.ARCHIVE:
                return session.kind is SessionKind.GHOST
            case Scope.ALL:
                return session.kind is not SessionKind.GHOST
        return True

    def _matches(self, session: Session) -> bool:
        if not self.query:
            return True
        needle = self.query.lower()
        if needle.startswith("#"):
            return any(needle[1:] in tag.lower() for tag in session.tags)
        haystack = " ".join(
            (session.title, str(session.project), session.last_prompt, session.note, session.sid,
             " ".join(session.tags))
        ).lower()
        return all(part in haystack for part in needle.split())

    def _sorted(self, rows: Iterable[Session]) -> list[Session]:
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        match self.sort:
            case SortKey.RECENT:
                return sorted(rows, key=lambda s: s.updated or epoch, reverse=True)
            case SortKey.OLDEST:
                return sorted(rows, key=lambda s: s.updated or epoch)
            case SortKey.PROJECT:
                return sorted(rows, key=lambda s: (str(s.project), -(s.updated or epoch).timestamp()))
            case SortKey.SIZE:
                return sorted(rows, key=lambda s: s.size_bytes, reverse=True)
            case SortKey.PROMPTS:
                return sorted(rows, key=lambda s: s.prompt_count, reverse=True)
            case SortKey.TITLE:
                return sorted(rows, key=lambda s: s.title.lower())
        return list(rows)

    def rebuild(self, keep_sid: str | None = None) -> None:
        if not self._ui_ready:
            return
        table = self.query_one("#table", DataTable)
        keep_sid = keep_sid or (self.current.sid if self.current else None)
        self.view = self._sorted(s for s in self.sessions if self._in_scope(s) and self._matches(s))

        table.clear()
        for session in self.view:
            table.add_row(
                _KIND_MARK[session.kind],
                _STATE_MARK[session.run_state],
                "[yellow]★[/yellow]" if session.favorite else " ",
                _ago(session.updated),
                session.project_label,
                self._title_cell(session),
                str(session.prompt_count),
                key=session.sid,
            )
        if keep_sid is not None:
            position = next((i for i, s in enumerate(self.view) if s.sid == keep_sid), None)
            if position is not None:
                table.move_cursor(row=position)
        self._update_status()
        self._update_detail()

    def _title_cell(self, session: Session) -> str:
        title = session.title.replace("[", "\\[")
        if session.tags:
            title += " " + " ".join(f"[dim yellow]#{t}[/dim yellow]" for t in session.tags)
        if session.kind is SessionKind.GHOST:
            return f"[dim]{title}[/dim]"
        return title

    def _update_status(self) -> None:
        live = sum(1 for s in self.sessions if s.kind is SessionKind.LIVE)
        ghosts = sum(1 for s in self.sessions if s.kind is SessionKind.GHOST)
        mat = sum(1 for s in self.sessions if s.kind is SessionKind.MATERIALIZED)
        running = sum(1 for s in self.sessions if s.run_state is RunState.RUNNING)
        favs = sum(1 for s in self.sessions if s.favorite)
        self.query_one("#status", Static).update(
            f"показано {len(self.view)}  ·  живых {live} · восстановлено {mat} · "
            f"призраков {ghosts} · запущено {running} · ★{favs}  ·  "
            f"сортировка: {_SORT_LABELS[self.sort]}  ·  [s] сменить"
        )

    @property
    def current(self) -> Session | None:
        table = self.query_one("#table", DataTable)
        if not self.view or table.cursor_row < 0 or table.cursor_row >= len(self.view):
            return None
        return self.view[table.cursor_row]

    def _update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        session = self.current
        detail.update(preview_markup(session) if session else "[dim]нет сессий в этой вкладке[/dim]")

    @on(DataTable.RowHighlighted)
    def _row_moved(self) -> None:
        self._update_detail()

    @on(DataTable.RowSelected)
    def _row_chosen(self) -> None:
        self.action_open()

    @on(Tabs.TabActivated)
    def _tab_changed(self, event: Tabs.TabActivated) -> None:
        if event.tab is None:
            return
        self.scope = Scope(event.tab.id)
        self.rebuild()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.query = event.value.strip()
        self.rebuild()

    @on(Input.Submitted, "#search")
    def _search_done(self) -> None:
        self.query_one("#table", DataTable).focus()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        self.query_one("#table", DataTable).focus()

    def action_scope(self, scope: str) -> None:
        self.query_one(Tabs).active = scope

    def action_sort(self) -> None:
        order = list(SortKey)
        self.sort = order[(order.index(self.sort) + 1) % len(order)]
        self.rebuild()

    def action_refresh(self) -> None:
        self.notify("пересканирую…", timeout=2)
        self.refresh_index()

    def action_favorite(self) -> None:
        session = self.current
        if session is None:
            return
        session.favorite = self.store.toggle(session.sid)
        self.notify(f"{'★ в избранном' if session.favorite else 'убрано из избранного'}: {session.title[:40]}")
        self.rebuild(keep_sid=session.sid)

    def action_note(self) -> None:
        session = self.current
        if session is None:
            return

        def save(value: str | None) -> None:
            if value is None:
                return
            self.store.set_note(session.sid, value)
            session.note, session.favorite = value, True
            self.rebuild(keep_sid=session.sid)

        self.push_screen(TextPrompt("Заметка к сессии", session.note), save)

    def action_tags(self) -> None:
        session = self.current
        if session is None:
            return

        def save(value: str | None) -> None:
            if value is None:
                return
            tags = [t for t in value.replace(",", " ").split() if t]
            self.store.set_tags(session.sid, tags)
            session.tags, session.favorite = tuple(sorted(set(tags))), True
            self.rebuild(keep_sid=session.sid)

        self.push_screen(TextPrompt("Теги через пробел", " ".join(session.tags)), save)

    def action_copy(self) -> None:
        session = self.current
        if session is None:
            return
        command = resume_command(session)
        self.notify(f"скопировано: {command}" if copy_to_clipboard(command) else "буфер недоступен")

    def action_materialize(self) -> None:
        session = self.current
        if session is None:
            return
        if session.kind is not SessionKind.GHOST:
            self.notify("это не призрак — материализация не нужна", severity="warning")
            return
        try:
            path = materialize(session)
        except (MaterializeError, OSError) as exc:
            self.notify(f"не вышло: {exc}", severity="error", timeout=8)
            return
        self.notify(f"восстановлено → {path.name}", timeout=6)
        self.refresh_index()

    def action_kill(self) -> None:
        session = self.current
        if session is None:
            return
        if kill_session(session):
            self.notify(f"остановлена: {session.title[:40]}")
            self.refresh_index()
        else:
            self.notify("нечего останавливать", severity="warning")

    def action_open(self) -> None:
        session = self.current
        if session is None:
            return
        if session.kind is SessionKind.GHOST:
            self.notify("призрак нельзя открыть — сначала [b]m[/b]", severity="warning")
            return
        self.store.mark_opened(session.sid)
        if self.select_to is not None:
            self.select_to.write_text(f"{session.sid}\n{session.project}\n", encoding="utf-8")
            self.exit(session)
            return
        try:
            if not focus_existing(session):
                open_window(session)
        except LaunchError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.exit(session)


def main(select_to: Path | None = None) -> int:
    return 0 if ClaudevPicker(select_to=select_to).run() is None else 0
