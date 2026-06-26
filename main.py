#!/usr/bin/env python3
import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

import apis.bluesky as bluesky_api
import apis.mastodon as mastodon_api
import apis.rss as rss_api
import apis.telegram as telegram_api
import apis.threads as threads_api
import apis.twitter as twitter_api
from config import WATCH_INTERVAL_MINUTES
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
""",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        help="Backfill: sync posts since this date (YYYY-MM-DD). Skips min-age filter.",
    )
    parser.add_argument(
        "--auth",
        choices=["twitter", "telegram", "mastodon", "threads", "bluesky", "rss"],
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


async def watch_mode(engine) -> None:
    interval_seconds = WATCH_INTERVAL_MINUTES * 60
    logging.info("Watch mode: checking every %d minutes", WATCH_INTERVAL_MINUTES)

    while True:
        try:
            count = await run_sync(engine, enforce_min_age=True)
            logging.info("Sync cycle complete — %d post(s) synced", count)
        except Exception:
            logging.exception("Sync cycle failed")

        logging.info("Sleeping for %d minutes...", WATCH_INTERVAL_MINUTES)
        await asyncio.sleep(interval_seconds)


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
