import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from utils.http_utils import format_api_error, parse_error_detail
from utils.text_utils import strip_trailing_patterns
from apis.types import MediaItem, OutboundPost, Post, PublishResult
from utils.urls import public_https_url
from utils.posts import sort_chronologically
from config import NETWORK_THREADS, THREADS_APP
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credential,
    set_credentials,
    update_remote_id,
)

logger = logging.getLogger(__name__)

THREADS_CAROUSEL_CHILD_DELAY_SECONDS = 2
THREADS_PUBLISH_WAIT_SECONDS = 5

AUTH_HELP = """\
Configure Threads account for mesh sync.

You will be asked for:
  1. Access Token — from https://developers.facebook.com/apps/
       → your app → Threads API → generate a User access token
       → required scopes: threads_basic, threads_content_publish
  2. (optional) Username — your @handle; looked up automatically if omitted

The token and profile are stored per account label in the local SQLite database.

Note: Threads API limits publishing to 250 posts per 24 hours per profile.
Media must be reachable via public HTTPS URL when posting images/videos.
"""

THREAD_FIELDS = (
    "id,text,timestamp,media_type,media_url,thumbnail_url,"
    "children{media_type,media_url,thumbnail_url},"
    "is_quote_post,reposted_post,root_post,replied_to,is_reply,owner,username"
)

_TRAILING_THREADS_URL = re.compile(
    r"\s+https?://(?:www\.)?threads\.(?:net|com)/\S+\s*$", re.IGNORECASE
)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00").replace("+0000", "+00:00")
    return datetime.fromisoformat(normalized)


def _strip_trailing_links(text: str, *, has_media: bool) -> str:
    if not has_media:
        return text.rstrip()
    return strip_trailing_patterns(text, [_TRAILING_THREADS_URL])


def _threads_media_type(media_type: str) -> str:
    if media_type in ("VIDEO", "GIF"):
        return "video"
    return "photo"


def _extract_media(item: dict[str, Any]) -> list[MediaItem]:
    media_type = item.get("media_type", "TEXT_POST")
    if media_type == "TEXT_POST":
        return []

    if media_type == "CAROUSEL_ALBUM":
        items: list[MediaItem] = []
        for child in item.get("children", {}).get("data", []):
            items.extend(_extract_media(child))
        return items

    if media_type in ("IMAGE", "VIDEO", "GIF"):
        url = item.get("media_url") or item.get("thumbnail_url")
        if not url:
            return []
        return [
            MediaItem(
                url=url,
                media_type=_threads_media_type(media_type),
                alt_text=item.get("alt_text"),
            )
        ]

    return []


def _skip_reason(item: dict[str, Any]) -> str | None:
    if item.get("is_quote_post"):
        return "quote"
    if item.get("reposted_post") or item.get("media_type") == "REPOST_FACADE":
        return "repost"
    if item.get("text", "").lstrip().startswith("@"):
        return "at_reply"
    return None


def _item_to_post(item: dict[str, Any], author_id: str) -> Post:
    media = _extract_media(item)
    text = _strip_trailing_links(item.get("text", ""), has_media=bool(media))

    root_post = item.get("root_post")
    if item.get("is_reply") and root_post:
        conversation_id = str(
            root_post.get("id", root_post) if isinstance(root_post, dict) else root_post
        )
        replied_to = item.get("replied_to")
        in_reply_to_id = (
            str(replied_to.get("id", replied_to))
            if isinstance(replied_to, dict)
            else str(replied_to) if replied_to else None
        )
    else:
        conversation_id = str(item["id"])
        in_reply_to_id = None

    return Post(
        id=str(item["id"]),
        text=text,
        created_at=_parse_timestamp(item["timestamp"]),
        conversation_id=conversation_id,
        author_id=str(author_id),
        media=media,
        in_reply_to_id=in_reply_to_id,
        in_reply_to_user_id=str(author_id) if in_reply_to_id else None,
        is_thread_root=conversation_id == str(item["id"]),
    )



def _require_creds(engine: Engine, account_id: int) -> dict[str, str]:
    creds = get_all_credentials(engine, account_id)
    missing = [k for k in ("access_token", "user_id") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"Threads account {account_id} not configured (missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=threads"
        )
    return creds


async def _api_get(
    access_token: str,
    path: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    query = dict(params or {})
    query["access_token"] = access_token
    url = f"{THREADS_APP.api_base_url}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, params=query)
        if not response.is_success:
            detail = parse_error_detail(response)
            raise RuntimeError(
                format_api_error("Threads", response.status_code, detail)
            )
        return response.json()


async def _api_post_form(
    access_token: str,
    path: str,
    data: dict[str, str],
) -> dict[str, Any]:
    url = f"{THREADS_APP.api_base_url}/{path.lstrip('/')}"
    payload = {**data, "access_token": access_token}
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, data=payload)
        if not response.is_success:
            detail = parse_error_detail(response)
            raise RuntimeError(
                format_api_error("Threads", response.status_code, detail)
            )
        return response.json()


async def _lookup_profile(access_token: str) -> dict[str, Any]:
    data = await _api_get(access_token, "me", params={"fields": "id,username"})
    if not data.get("id"):
        raise RuntimeError("Threads profile lookup failed: missing id")
    return data


async def _ensure_profile(engine: Engine, account_id: int) -> dict[str, str]:
    creds = _require_creds(engine, account_id)
    if creds.get("username"):
        return creds

    profile = await _lookup_profile(creds["access_token"])
    creds = {
        **creds,
        "user_id": str(profile["id"]),
        "username": profile.get("username", ""),
    }
    set_credential(engine, account_id, "user_id", creds["user_id"])
    if creds["username"]:
        set_credential(engine, account_id, "username", creds["username"])
    update_remote_id(engine, account_id, creds["user_id"])
    return creds


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    access_token = input("Access Token: ").strip()
    username = input("Username (optional, press Enter to auto-detect): ").strip().lstrip("@")

    if not access_token:
        raise RuntimeError("Access token is required.")

    profile = await _lookup_profile(access_token)
    creds = {
        "access_token": access_token,
        "user_id": str(profile["id"]),
        "username": username or profile.get("username", ""),
    }

    existing = find_account(engine, NETWORK_THREADS, label)
    if existing:
        set_credentials(engine, existing.id, creds)
        update_remote_id(engine, existing.id, creds["user_id"])
        print(f"Threads account '{label}' updated for @{creds['username']}")
        return existing

    account = create_account(engine, NETWORK_THREADS, label, creds["user_id"])
    set_credentials(engine, account.id, creds)
    print(f"Threads account '{label}' configured for @{creds['username']}")
    return account


async def _fetch_endpoint(
    access_token: str,
    user_id: str,
    endpoint: str,
    *,
    since: datetime | None,
    max_pages: int | None,
) -> list[dict[str, Any]]:
    since_utc = since.astimezone(timezone.utc) if since else None
    params: dict[str, str] = {
        "fields": THREAD_FIELDS,
        "limit": "50",
    }
    if since_utc:
        params["since"] = since_utc.strftime("%Y-%m-%d")

    items: list[dict[str, Any]] = []
    after: str | None = None
    page = 0

    while True:
        page += 1
        page_params = dict(params)
        if after:
            page_params["after"] = after

        data = await _api_get(access_token, f"{user_id}/{endpoint}", page_params)
        batch = data.get("data") or []
        reached_since = False

        for item in batch:
            created_at = _parse_timestamp(item["timestamp"])
            if since_utc and created_at < since_utc:
                reached_since = True
                continue
            items.append(item)

        paging = data.get("paging", {})
        cursors = paging.get("cursors", {})
        after = cursors.get("after")
        if reached_since or not after:
            break
        if max_pages is not None and page >= max_pages:
            break

    return items


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    creds = await _ensure_profile(engine, account_id)
    access_token = creds["access_token"]
    user_id = creds["user_id"]

    raw_items = await _fetch_endpoint(
        access_token, user_id, "threads", since=since, max_pages=max_pages
    )
    if include_replies:
        raw_items.extend(
            await _fetch_endpoint(
                access_token, user_id, "replies", since=since, max_pages=max_pages
            )
        )

    skipped: dict[str, int] = {}
    posts: list[Post] = []
    seen_ids: set[str] = set()

    for item in raw_items:
        item_id = str(item.get("id", ""))
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        reason = _skip_reason(item)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        posts.append(_item_to_post(item, user_id))

    if skipped:
        logger.info(
            "Threads fetch skipped: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())),
        )

    return sort_chronologically(posts)


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        response.raise_for_status()
        return response.content


async def _create_container(
    access_token: str,
    user_id: str,
    *,
    text: str,
    media: list[MediaItem],
    reply_to_id: str | None = None,
) -> str:
    public_media = [m for m in media if public_https_url(m.url)]
    private = [m for m in media if m not in public_media]
    if private:
        logger.warning(
            "Threads publish: skipping %d non-public media URL(s) (e.g. tgfile:)",
            len(private),
        )

    if not public_media:
        data: dict[str, str] = {"media_type": "TEXT", "text": text or ""}
    elif len(public_media) == 1:
        item = public_media[0]
        if item.media_type == "photo":
            data = {
                "media_type": "IMAGE",
                "image_url": item.url,
                "text": text or "",
            }
        else:
            data = {
                "media_type": "VIDEO",
                "video_url": item.url,
                "text": text or "",
            }
    else:
        child_ids: list[str] = []
        for item in public_media[:20]:
            if item.media_type == "photo":
                child_data = {
                    "media_type": "IMAGE",
                    "image_url": item.url,
                    "is_carousel_item": "true",
                }
            else:
                child_data = {
                    "media_type": "VIDEO",
                    "video_url": item.url,
                    "is_carousel_item": "true",
                }
            child = await _api_post_form(
                access_token, f"{user_id}/threads", child_data
            )
            child_ids.append(str(child["id"]))
            await asyncio.sleep(THREADS_CAROUSEL_CHILD_DELAY_SECONDS)

        data = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "text": text or "",
        }

    if reply_to_id:
        data["reply_to_id"] = reply_to_id

    if media and not public_media:
        logger.warning(
            "Threads publish: no public media URLs — posting text only"
        )

    result = await _api_post_form(access_token, f"{user_id}/threads", data)
    container_id = result.get("id")
    if not container_id:
        raise RuntimeError(f"Threads create container: missing id in {result}")
    return str(container_id)


async def _publish_container(
    access_token: str, user_id: str, container_id: str
) -> str:
    await asyncio.sleep(THREADS_PUBLISH_WAIT_SECONDS)
    result = await _api_post_form(
        access_token,
        f"{user_id}/threads_publish",
        {"creation_id": container_id},
    )
    post_id = result.get("id")
    if not post_id:
        raise RuntimeError(f"Threads publish: missing id in {result}")
    return str(post_id)


async def publish_outbound(
    engine: Engine,
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    if media_bytes:
        logger.debug(
            "Threads publish ignores downloaded bytes — API requires public HTTPS URLs"
        )
    creds = await _ensure_profile(engine, account_id)
    access_token = creds["access_token"]
    user_id = creds["user_id"]

    container_id = await _create_container(
        access_token,
        user_id,
        text=outbound.text,
        media=outbound.media,
        reply_to_id=reply_to,
    )
    post_id = await _publish_container(access_token, user_id, container_id)
    return PublishResult(post_id=post_id, reply_ref=post_id)