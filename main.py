#!/usr/bin/env python3
import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from croniter import croniter

from apis.registry import NETWORK_APIS
from config import ACCOUNT_SYNC_ROLES, WATCH_CRON
from db.accounts import account_display_name, find_account, list_accounts, set_account_role
from db.connection import get_engine
from db.projects import configure_project, create_project, find_project, list_projects
from sync.engine import run_sync


def parse_since(value: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date format: {value!r}. Use YYYY-MM-DD."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mesh-sync posts across social networks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Use --auth NETWORK --label NAME to configure each account.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--watch", action="store_true", help="Run continuous scheduled sync instead of the interactive TUI.")
    actions.add_argument(
        "--since",
        type=parse_since,
        help="Backfill: sync posts since this date (YYYY-MM-DD). Skips min-age filter.",
    )
    actions.add_argument(
        "--auth",
        choices=list(NETWORK_APIS),
        metavar="NETWORK",
        help="Configure a network account and store credentials in SQLite.",
    )
    parser.add_argument(
        "--label",
        default="default",
        help="Account label within a project and network (default: default). Use to add multiple accounts.",
    )
    actions.add_argument(
        "--list-accounts",
        action="store_true",
        help="List configured accounts and exit.",
    )
    actions.add_argument(
        "--migrate",
        action="store_true",
        help="Apply pending database migrations and exit.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument("--sync-role", choices=ACCOUNT_SYNC_ROLES, help="Connection role for --auth or --configure-account; reauthentication preserves the current role when omitted.")
    actions.add_argument("--configure-account", choices=list(NETWORK_APIS), metavar="NETWORK", help="Change the role of --project/--label without reauthenticating.")
    parser.add_argument("--project", metavar="NAME", help="Select a project; auth defaults to Default, sync defaults to all enabled projects.")
    actions.add_argument("--create-project", metavar="NAME", help="Create an empty project and exit.")
    actions.add_argument("--list-projects", action="store_true", help="List project settings and sync state.")
    actions.add_argument("--configure-project", action="store_true", help="Update settings for --project NAME and exit.")
    parser.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None, help="Enable or disable the selected project.")
    parser.add_argument("--min-age-minutes", type=int)
    parser.add_argument("--overlap-hours", type=int)
    parser.add_argument("--initial-lookback-hours", type=int)
    parser.add_argument("--backfill-delay-seconds", type=float)
    return parser


def seconds_until_next_cron_run(
    cron_expr: str, *, now: datetime | None = None
) -> tuple[float, datetime]:
    now = now or datetime.now(timezone.utc)
    next_run = croniter(cron_expr, now).get_next(datetime)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    delay = max(0.0, (next_run - now).total_seconds())
    return delay, next_run


async def watch_mode(engine, project_id: int | None = None) -> None:
    logging.info("Watch mode: cron %s (UTC)", WATCH_CRON)
    # First cycle after start/restart (e.g. docker compose up) skips min-age
    # so recent posts are not held until they age past POST_MIN_AGE_MINUTES.
    first_cycle = True

    while True:
        if first_cycle:
            logging.info("First run after start — ignoring POST_MIN_AGE_MINUTES")
        try:
            count = await run_sync(engine, enforce_min_age=not first_cycle, project_id=project_id)
            logging.info("Sync cycle complete — %d post(s) synced", count)
        except Exception:
            logging.exception("Sync cycle failed")
        first_cycle = False

        delay, next_run = seconds_until_next_cron_run(WATCH_CRON)
        logging.info(
            "Next sync at %s UTC (in %.0f seconds)",
            next_run.strftime("%Y-%m-%d %H:%M"),
            delay,
        )
        await asyncio.sleep(delay)


async def async_main(args: argparse.Namespace) -> None:
    if args.sync_role is not None and not (args.auth or args.configure_account):
        raise ValueError("--sync-role requires --auth or --configure-account")
    if args.configure_account and args.sync_role is None:
        raise ValueError("--configure-account requires --sync-role")
    settings = {
        "enabled": args.enabled, "min_age_minutes": args.min_age_minutes,
        "overlap_hours": args.overlap_hours, "initial_lookback_hours": args.initial_lookback_hours,
        "backfill_delay_seconds": args.backfill_delay_seconds,
    }
    if any(value is not None for value in settings.values()) and not args.configure_project:
        raise ValueError("Project settings require --configure-project --project NAME")
    if args.configure_project and not args.project:
        raise ValueError("--configure-project requires --project NAME")
    if args.project and (args.create_project or args.list_projects or args.migrate):
        raise ValueError("--project cannot be combined with --create-project, --list-projects, or --migrate")
    interactive = not any((args.watch, args.auth, args.since, args.migrate, args.list_accounts,
                           args.configure_account, args.create_project, args.list_projects, args.configure_project))
    if interactive and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("The TUI needs an interactive terminal. Use docker compose exec -it bot uv run python main.py, or --watch for unattended sync.")
    engine = get_engine()

    if args.migrate:
        logging.info("Database migrations up to date: %s", engine.url)
        return

    if args.create_project:
        project = create_project(engine, args.create_project)
        print(f"Created project {project.name} ({project.id})")
        return

    if args.list_projects:
        for project in list_projects(engine):
            print(
                f"{project.id}: {project.name} enabled={project.enabled} "
                f"min_age_minutes={project.min_age_minutes} overlap_hours={project.overlap_hours} "
                f"initial_lookback_hours={project.initial_lookback_hours} backfill_delay_seconds={project.backfill_delay_seconds} "
                f"last_synced_at={project.last_synced_at} last_sync_finished_at={project.last_sync_finished_at} "
                f"count={project.last_sync_count} errors={project.last_sync_error_count}"
            )
        return

    project = find_project(engine, args.project) if args.project else None
    if args.configure_project:
        configure_project(engine, project.id, **settings)
        return

    if args.list_accounts:
        accounts = list_accounts(engine, project_id=project.id if project else None)
        names = {item.id: item.name for item in list_projects(engine)}
        if not accounts:
            print("No accounts configured.")
            return
        for account in accounts:
            print(
                f"{account.id}: {names[account.project_id]} / {account.network}/{account.label} "
                f"[{account.sync_role}] ({account_display_name(account, engine)})"
            )
        return

    if args.configure_account:
        selected = project or find_project(engine, "Default")
        account = find_account(engine, args.configure_account, args.label, project_id=selected.id)
        if account is None:
            raise ValueError(f"No connection {args.configure_account}/{args.label} in {selected.name}")
        set_account_role(engine, account.id, args.sync_role)
        return

    if args.auth:
        await NETWORK_APIS[args.auth].authenticate(
            engine, label=args.label, project_id=(project or find_project(engine, "Default")).id,
            sync_role=args.sync_role,
        )
        return

    if args.since:
        count = await run_sync(engine, since=args.since, enforce_min_age=False, project_id=project.id if project else None)
        logging.info("Backfill complete — %d post(s) synced", count)
        return

    if args.watch:
        await watch_mode(engine, project_id=project.id if project else None)
    else:
        from tui.app import BotApp
        await BotApp(engine, project_id=project.id if project else None).run_async()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(async_main(args))
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        logging.info("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
