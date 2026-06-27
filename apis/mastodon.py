import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post, sort_chronologically
from config import NETWORK_MASTODON
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credentials,
)

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure Mastodon account for mesh sync.

You will be asked for:
  1. Instance URL — your server's base URL (e.g. https://mastodon.social)
  2. Access Token — from your instance:
       Preferences → Development → Your application → Access token
       (create an app with read + write scopes if you don't have one yet)

Credentials are stored per account label in the local SQLite database.
"""


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _get_credentials(engine: Engine, account_id: int) -> tuple[str, str]:
    creds = get_all_credentials(engine, account_id)
    instance_url = creds.get("instance_url", "").rstrip("/")
    access_token = creds.get("access_token")
    if not instance_url or not access_token:
        raise RuntimeError(
            f"Mastodon account {account_id} not configured. "
            "Run: python main.py --auth=mastodon"
        )
    return instance_url, access_token


def _api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return body.get("error", response.text)
    except Exception:
        pass
    return response.text


def _extract_media(status: dict[str, Any]) -> list[MediaItem]:
    items: list[MediaItem] = []
    for attachment in status.get("media_attachments") or []:
        media_type = attachment.get("type", "image")
        if media_type == "image":
            media_type = "photo"
        elif media_type == "gifv":
            media_type = "animated_gif"
        url = attachment.get("url") or attachment.get("preview_url")
        if not url:
            continue
        items.append(
            MediaItem(
                url=url,
                media_type=media_type,
                alt_text=attachment.get("description"),
            )
        )
    return items


def _status_to_post(status: dict[str, Any], author_id: str) -> Post | None:
    if status.get("reblog"):
        return None
    if status.get("in_reply_to_account_id") and str(
        status.get("in_reply_to_account_id")
    ) != str(author_id):
        return None

    text = status.get("content") or ""
    if text.startswith("<"):
        text = _strip_html(text)
    media = _extract_media(status)
    if not text.strip() and not media:
        return None

    status_id = str(status["id"])
    conversation_id = (
        str(status["in_reply_to_id"])
        if status.get("in_reply_to_id")
        and str(status.get("in_reply_to_account_id")) == str(author_id)
        else status_id
    )

    return Post(
        id=status_id,
        text=text.strip(),
        created_at=_parse_datetime(status["created_at"]),
        conversation_id=conversation_id,
        author_id=str(author_id),
        media=media,
        in_reply_to_id=str(status["in_reply_to_id"])
        if status.get("in_reply_to_id")
        else None,
        in_reply_to_user_id=str(status.get("in_reply_to_account_id"))
        if status.get("in_reply_to_account_id")
        else None,
        is_thread_root=conversation_id == status_id,
    )


def _normalize_thread_roots(posts: list[Post]) -> list[Post]:
    """Resolve Mastodon reply chains to a shared conversation root."""
    by_id = {post.id: post for post in posts}

    def root_for(post: Post) -> str:
        current = post
        seen: set[str] = set()
        while current.in_reply_to_id and current.in_reply_to_id in by_id:
            if current.id in seen:
                break
            seen.add(current.id)
            current = by_id[current.in_reply_to_id]
        return current.id

    normalized: list[Post] = []
    for post in posts:
        root_id = root_for(post)
        normalized.append(
            Post(
                id=post.id,
                text=post.text,
                created_at=post.created_at,
                conversation_id=root_id,
                author_id=post.author_id,
                media=post.media,
                in_reply_to_id=post.in_reply_to_id,
                in_reply_to_user_id=post.in_reply_to_user_id,
                is_thread_root=post.id == root_id,
            )
        )
    return normalized


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _media_filename(item: MediaItem, index: int) -> str:
    if item.media_type == "photo":
        return f"photo{index}.jpg"
    if item.media_type == "animated_gif":
        return f"animation{index}.mp4"
    return f"video{index}.mp4"


def _media_content_type(item: MediaItem) -> str:
    if item.media_type == "photo":
        return "image/jpeg"
    return "video/mp4"


async def _upload_media(
    client: httpx.AsyncClient,
    instance_url: str,
    access_token: str,
    item: MediaItem,
    raw: bytes,
    index: int,
) -> str:
    data: dict[str, str] = {}
    if item.alt_text:
        data["description"] = item.alt_text

    response = await client.post(
        f"{instance_url}/api/v1/media",
        headers={"Authorization": f"Bearer {access_token}"},
        data=data,
        files={
            "file": (
                _media_filename(item, index),
                raw,
                _media_content_type(item),
            )
        },
    )
    if not response.is_success:
        raise RuntimeError(f"Mastodon media upload: {_api_error(response)}")
    media_id = response.json().get("id")
    if not media_id:
        raise RuntimeError("Mastodon media upload: missing id in response")
    return str(media_id)


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    instance_url = input("Instance URL: ").strip().rstrip("/")
    access_token = input("Access Token: ").strip()

    if not instance_url or not access_token:
        raise RuntimeError("Instance URL and access token are required.")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{instance_url}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not response.is_success:
            raise RuntimeError(
                f"Mastodon verify_credentials: {_api_error(response)}"
            )
        account_data = response.json()

    host = urlparse(instance_url).hostname or instance_url
    creds = {
        "instance_url": instance_url,
        "access_token": access_token,
        "username": account_data["username"],
        "account_id": str(account_data["id"]),
    }

    existing = find_account(engine, NETWORK_MASTODON, label)
    if existing:
        set_credentials(engine, existing.id, creds)
        print(
            f"Mastodon account '{label}' updated for "
            f"@{account_data['username']}@{host}"
        )
        return existing

    account = create_account(
        engine, NETWORK_MASTODON, label, str(account_data["id"])
    )
    set_credentials(engine, account.id, creds)
    print(
        f"Mastodon account '{label}' configured for "
        f"@{account_data['username']}@{host}"
    )
    return account


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    instance_url, access_token = await _get_credentials(engine, account_id)
    creds = get_all_credentials(engine, account_id)
    remote_account_id = creds.get("account_id")
    if not remote_account_id:
        raise RuntimeError(f"Mastodon account {account_id} missing account_id credential")

    since_utc = since.astimezone(timezone.utc) if since else None
    headers = {"Authorization": f"Bearer {access_token}"}
    all_posts: list[Post] = []
    max_id: str | None = None
    page = 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            params: dict[str, str] = {"limit": "40", "exclude_reblogs": "true"}
            if not include_replies:
                params["exclude_replies"] = "true"
            if max_id:
                params["max_id"] = max_id

            response = await client.get(
                f"{instance_url}/api/v1/accounts/{remote_account_id}/statuses",
                headers=headers,
                params=params,
            )
            if not response.is_success:
                raise RuntimeError(f"Mastodon fetch statuses: {_api_error(response)}")

            statuses = response.json()
            if not statuses:
                break

            reached_since = False
            for status in statuses:
                created_at = _parse_datetime(status["created_at"])
                if since_utc and created_at < since_utc:
                    reached_since = True
                    continue
                post = _status_to_post(status, remote_account_id)
                if post:
                    all_posts.append(post)

            max_id = str(statuses[-1]["id"])
            if reached_since:
                break
            if max_pages is not None and page >= max_pages:
                break
            if len(statuses) < 40:
                break

    all_posts = sort_chronologically(all_posts)
    return _normalize_thread_roots(all_posts)


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
    in_reply_to_id: str | None = None,
) -> str:
    instance_url, access_token = await _get_credentials(engine, account_id)
    text = outbound.text or ""
    media = outbound.media
    bytes_list = media_bytes or []
    log_id = outbound.source_post_ids[0] if outbound.source_post_ids else "?"

    if media and len(bytes_list) != len(media):
        raise RuntimeError(
            f"Media upload mismatch for post {log_id}: "
            f"{len(media)} attachment(s) but {len(bytes_list)} downloaded"
        )

    async with httpx.AsyncClient(timeout=120.0) as client:
        media_ids: list[str] = []
        for i, (item, raw) in enumerate(zip(media, bytes_list)):
            media_id = await _upload_media(
                client, instance_url, access_token, item, raw, i
            )
            media_ids.append(media_id)

        payload: dict[str, str | list[str]] = {"status": text or ""}
        if media_ids:
            payload["media_ids"] = media_ids
        if in_reply_to_id:
            payload["in_reply_to_id"] = in_reply_to_id

        response = await client.post(
            f"{instance_url}/api/v1/statuses",
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
        )
        if not response.is_success:
            raise RuntimeError(f"Mastodon post status: {_api_error(response)}")
        status_id = response.json().get("id")
        if not status_id:
            raise RuntimeError("Mastodon post status: missing id in response")
        return str(status_id)


async def publish_post(
    engine: Engine,
    account_id: int,
    post: Post,
    media_bytes: list[bytes] | None = None,
) -> str:
    outbound = OutboundPost(
        text=post.text,
        media=post.media,
        source_post_ids=[post.id],
    )
    return await publish_outbound(engine, account_id, outbound, media_bytes)
