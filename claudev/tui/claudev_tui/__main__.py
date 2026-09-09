"""CLI entry: `claudev-pick` opens the TUI, subcommands cover scripted use."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import RunState, SessionKind
from .reconstruct import Confidence, reconstruct, render_brief
from .scanner import CLAUDE_HOME, build_index
from .store import FavoriteStore


def _index():
    sessions = build_index()
    FavoriteStore().annotate(sessions)
    return sessions


def cmd_tui(args: argparse.Namespace) -> int:
    from .app import main

    return main(Path(args.out) if getattr(args, "out", None) else None)


def cmd_list(args: argparse.Namespace) -> int:
    sessions = _index()
    if args.ghosts:
        sessions = [s for s in sessions if s.kind is SessionKind.GHOST]
    if args.favorites:
        sessions = [s for s in sessions if s.favorite]
    if args.json:
        print(json.dumps([
            {
                "sid": s.sid, "project": str(s.project), "kind": s.kind.value,
                "title": s.title, "run_state": s.run_state.value,
                "updated": s.updated.isoformat() if s.updated else None,
                "prompts": s.prompt_count, "favorite": s.favorite, "tags": list(s.tags),
            }
            for s in sessions
        ], ensure_ascii=False, indent=2))
        return 0
    for s in sessions:
        star = "★" if s.favorite else " "
        run = "▶" if s.run_state is RunState.RUNNING else " "
        stamp = f"{s.updated:%Y-%m-%d %H:%M}" if s.updated else "—"
        print(f"{star}{run} {s.kind.value[:4]:5} {stamp} {s.project_label:24.24} "
              f"{s.prompt_count:4}p  {s.title[:60]}")
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    from .materialize import MaterializeError, materialize

    sessions = [s for s in _index() if s.kind is SessionKind.GHOST]
    if args.sid:
        targets = [s for s in sessions if s.sid.startswith(args.sid)]
        if not targets:
            print(f"призрак {args.sid} не найден", file=sys.stderr)
            return 1
    else:
        targets = [
            s for s in sessions
            if not args.strong_only or reconstruct(s).confidence is Confidence.STRONG
        ]
    failures = 0
    for session in targets:
        try:
            path = materialize(session, force=args.force)
            print(f"ok   {session.sid[:8]} {session.project_label:24.24} → {path}")
        except (MaterializeError, OSError) as exc:
            failures += 1
            print(f"пропуск {session.sid[:8]}: {exc}", file=sys.stderr)
    print(f"\nвосстановлено {len(targets) - failures} из {len(targets)}")
    return 1 if failures == len(targets) and targets else 0


def cmd_brief(args: argparse.Namespace) -> int:
    match = [s for s in _index() if s.sid.startswith(args.sid)]
    if not match:
        print(f"сессия {args.sid} не найдена", file=sys.stderr)
        return 1
    print(render_brief(reconstruct(match[0])))
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = CLAUDE_HOME / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[!] settings.json нечитаем: {exc}")
        return 1
    retention = data.get("cleanupPeriodDays")
    if retention is None:
        print("[!] cleanupPeriodDays НЕ ЗАДАН — Claude Code удалит транскрипты старше 30 дней")
    elif retention < 365:
        print(f"[!] cleanupPeriodDays={retention} — меньше года, транскрипты будут вычищаться")
    else:
        print(f"[ok] cleanupPeriodDays={retention}")

    sessions = _index()
    ghosts = [s for s in sessions if s.kind is SessionKind.GHOST]
    strong = sum(1 for s in ghosts if reconstruct(s).confidence is Confidence.STRONG)
    print(f"[i] сессий: {len(sessions)}  живых: {sum(1 for s in sessions if s.kind is SessionKind.LIVE)}"
          f"  восстановленных: {sum(1 for s in sessions if s.kind is SessionKind.MATERIALIZED)}"
          f"  призраков: {len(ghosts)} (из них с git-доказательствами: {strong})")

    marker = Path.home() / ".local/state/claudev/last-backup"
    if marker.is_file():
        print(f"[ok] последний бэкап: {marker.read_text(encoding='utf-8').strip()}")
    else:
        print("[!] бэкапов ещё не было")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claudev-pick", description="Обзор и восстановление сессий Claude")
    parser.add_argument("--out", help="записать выбранный sid в файл и выйти, окно не открывать")
    sub = parser.add_subparsers(dest="command")

    p_tui = sub.add_parser("tui", help="интерактивный выбор (по умолчанию)")
    p_tui.add_argument("--out", help="записать выбранный sid в файл и выйти, окно не открывать")
    p_tui.set_defaults(func=cmd_tui)

    p_list = sub.add_parser("list", help="печать списка сессий")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--ghosts", action="store_true", help="только призраки")
    p_list.add_argument("--favorites", action="store_true", help="только избранное")
    p_list.set_defaults(func=cmd_list)

    p_mat = sub.add_parser("materialize", help="призрак → резюмируемая сессия")
    p_mat.add_argument("sid", nargs="?", help="префикс sid; без него — пакетно")
    p_mat.add_argument("--strong-only", action="store_true", help="только с git-доказательствами")
    p_mat.add_argument("--force", action="store_true", help="перезаписать существующий транскрипт")
    p_mat.set_defaults(func=cmd_materialize)

    p_brief = sub.add_parser("brief", help="досье реконструкции по сессии")
    p_brief.add_argument("sid")
    p_brief.set_defaults(func=cmd_brief)

    sub.add_parser("doctor", help="проверка настроек хранения и бэкапов").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        return cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
