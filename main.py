#!/usr/bin/env python3
import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from croniter import croniter

import apis.bluesky as bluesky_api
import apis.instagram as instagram_api
import apis.mastodon as mastodon_api
import apis.rss as rss_api
import apis.telegram as telegram_api
import apis.threads as threads_api
import apis.twitter as twitter_api
from config import WATCH_CRON
from db.accounts import account_display_name, list_accounts
from db.connection import get_engine
from sync.engine import run_sync


def _parse_since(value: str) -> datetime:
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
        epilog=f"""\
auth setup (use --label to connect multiple accounts per network):

{twitter_api.AUTH_HELP}
telegram:
  Prompts for bot token (@BotFather) and channel ID (@channel or -100…).

{mastodon_api.AUTH_HELP}
{threads_api.AUTH_HELP}
{bluesky_api.AUTH_HELP}
{rss_api.AUTH_HELP}
{instagram_api.AUTH_HELP}
""",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        help="Backfill: sync posts since this date (YYYY-MM-DD). Skips min-age filter.",
    )
    parser.add_argument(
        "--auth",
        choices=["twitter", "telegram", "mastodon", "threads", "bluesky", "rss", "instagram"],
        metavar="NETWORK",
        help="Configure a network account and store credentials in SQLite.",
    )
    parser.add_argument(
        "--label",
        default="default",
        help="Account label within a network (default: default). Use to add multiple accounts.",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="List configured accounts and exit.",
    )
    parser.add_argument(
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
    return parser


def _seconds_until_next_cron_run(
    cron_expr: str, *, now: datetime | None = None
) -> tuple[float, datetime]:
    now = now or datetime.now(timezone.utc)
    next_run = croniter(cron_expr, now).get_next(datetime)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    delay = max(0.0, (next_run - now).total_seconds())
    return delay, next_run


async def watch_mode(engine) -> None:
    logging.info("Watch mode: cron %s (UTC)", WATCH_CRON)

    while True:
        try:
            count = await run_sync(engine, enforce_min_age=True)
            logging.info("Sync cycle complete — %d post(s) synced", count)
        except Exception:
            logging.exception("Sync cycle failed")

        delay, next_run = _seconds_until_next_cron_run(WATCH_CRON)
        logging.info(
            "Next sync at %s UTC (in %s)",
            next_run.strftime("%Y-%m-%d %H:%M"),
            _format_duration(delay),
        )
        await asyncio.sleep(delay)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


async def async_main(args: argparse.Namespace) -> None:
    engine = get_engine()

    if args.migrate:
        logging.info("Database migrations up to date: %s", engine.url)
        return

    if args.list_accounts:
        accounts = list_accounts(engine)
        if not accounts:
            print("No accounts configured.")
            return
        for account in accounts:
            print(
                f"{account.id}: {account.network}/{account.label} "
                f"({account_display_name(account, engine)})"
            )
        return

    if args.auth == "twitter":
        await twitter_api.authenticate(engine, label=args.label)
        return
    if args.auth == "telegram":
        await telegram_api.authenticate(engine, label=args.label)
        return
    if args.auth == "mastodon":
        await mastodon_api.authenticate(engine, label=args.label)
        return
    if args.auth == "threads":
        await threads_api.authenticate(engine, label=args.label)
        return
    if args.auth == "bluesky":
        await bluesky_api.authenticate(engine, label=args.label)
        return
    if args.auth == "rss":
        await rss_api.authenticate(engine, label=args.label)
        return
    if args.auth == "instagram":
        await instagram_api.authenticate(engine, label=args.label)
        return

    if args.since:
        count = await run_sync(engine, since=args.since, enforce_min_age=False)
        logging.info("Backfill complete — %d post(s) synced", count)
        return

    await watch_mode(engine)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logging.info("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
