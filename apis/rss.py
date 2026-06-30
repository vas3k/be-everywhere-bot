import hashlib
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post, PublishResult
from utils.posts import sort_chronologically
from config import NETWORK_RSS
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credentials,
)

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure an RSS feed as a one-way sync source.

You will be asked for:
  1. Feed URL — the RSS or Atom feed URL (e.g. https://example.com/feed.xml)

New feed items are published to your social accounts as title, description,
and a link to the original post (no full article content or media).
Posting back to RSS is not supported — RSS is read-only in mesh sync.

Use --label to connect multiple feeds.
"""

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_TAG_RE = re.compile(r"<[^>]+>")


def _stable_post_id(*candidates: str | None) -> str:
    for value in candidates:
        if not value:
            continue
        text = value.strip()
        if not text:
            continue
        if len(text) <= 64:
            return text
        return hashlib.sha256(text.encode()).hexdigest()[:64]
    raise ValueError("RSS item has no guid, id, or link")


def _strip_html(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value))
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    value = value.strip()
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _item_text(title: str, description: str, link: str) -> str:
    """Build post text from title, short description, and original URL only."""
    title = title.strip()
    description = _strip_html(description)
    parts = [part for part in (title, description) if part]
    text = "\n\n".join(parts)
    if link and link not in text:
        text = f"{text}\n\n{link}" if text else link
    return text.strip()


def _parse_rss(root: ET.Element, feed_url: str) -> list[Post]:
    channel = root.find("channel")
    if channel is None:
        return []

    posts: list[Post] = []
    feed_id = _stable_post_id(
        (channel.findtext("title") or "").strip(),
        feed_url,
    )

    for item in channel.findall("item"):
        title = item.findtext("title") or ""
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        description = item.findtext("description") or ""
        pub_date = item.findtext("pubDate") or item.findtext("date")

        post_id = _stable_post_id(guid, link, title)
        text = _item_text(title, description, link)
        if not text:
            continue

        posts.append(
            Post(
                id=post_id,
                text=text,
                created_at=_parse_date(pub_date),
                conversation_id=post_id,
                author_id=feed_id,
                media=[],
                is_thread_root=True,
            )
        )
    return posts


def _atom_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    if element.text:
        return element.text.strip()
    return "".join(element.itertext()).strip()


def _parse_atom(root: ET.Element, feed_url: str) -> list[Post]:
    posts: list[Post] = []
    feed_title = _atom_text(root.find("atom:title", _ATOM_NS)) or feed_url
    feed_id = _stable_post_id(feed_title, feed_url)

    for entry in root.findall("atom:entry", _ATOM_NS):
        title = _atom_text(entry.find("atom:title", _ATOM_NS))
        entry_id = _atom_text(entry.find("atom:id", _ATOM_NS))
        link = ""
        for link_el in entry.findall("atom:link", _ATOM_NS):
            rel = link_el.attrib.get("rel", "alternate")
            if rel == "alternate" or not link:
                link = link_el.attrib.get("href", "")
        summary = _atom_text(entry.find("atom:summary", _ATOM_NS))
        published = _atom_text(entry.find("atom:published", _ATOM_NS)) or _atom_text(
            entry.find("atom:updated", _ATOM_NS)
        )

        post_id = _stable_post_id(entry_id, link, title)
        text = _item_text(title, summary, link)
        if not text:
            continue

        posts.append(
            Post(
                id=post_id,
                text=text,
                created_at=_parse_date(published),
                conversation_id=post_id,
                author_id=feed_id,
                media=[],
                is_thread_root=True,
            )
        )
    return posts


def _parse_feed(xml_text: str, feed_url: str) -> list[Post]:
    root = ET.fromstring(xml_text)
    tag = root.tag.rsplit("}", 1)[-1]
    if tag == "feed":
        return _parse_atom(root, feed_url)
    if tag == "rss":
        return _parse_rss(root, feed_url)
    raise RuntimeError(f"Unsupported feed format: {root.tag}")


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    feed_url = input("Feed URL: ").strip()
    if not feed_url:
        raise RuntimeError("Feed URL is required.")

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(feed_url)
        response.raise_for_status()
        posts = _parse_feed(response.text, feed_url)

    creds = {"feed_url": feed_url}
    remote_id = _stable_post_id(feed_url)

    existing = find_account(engine, NETWORK_RSS, label)
    if existing:
        set_credentials(engine, existing.id, creds)
        print(f"RSS feed '{label}' updated: {feed_url} ({len(posts)} item(s) in feed)")
        return existing

    account = create_account(engine, NETWORK_RSS, label, remote_id)
    set_credentials(engine, account.id, creds)
    print(f"RSS feed '{label}' configured: {feed_url} ({len(posts)} item(s) in feed)")
    return account


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    creds = get_all_credentials(engine, account_id)
    feed_url = creds.get("feed_url")
    if not feed_url:
        raise RuntimeError(
            f"RSS account {account_id} not configured. "
            "Run: uv run python main.py --auth=rss"
        )

    since_utc = since.astimezone(timezone.utc) if since else None

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(feed_url)
        response.raise_for_status()
        posts = _parse_feed(response.text, feed_url)

    if since_utc:
        posts = [p for p in posts if p.created_at >= since_utc]

    posts = sort_chronologically(posts)
    logger.info(
        "RSS feed %s: %d item(s)%s",
        feed_url,
        len(posts),
        f" since {since_utc.date()}" if since_utc else "",
    )
    return posts


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        response.raise_for_status()
        return response.content


async def publish_outbound(
    engine: Engine,
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    raise NotImplementedError("RSS is configured as a source-only feed")
