import logging
from urllib.parse import urlparse

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post
from config import NETWORK_MASTODON
from db.credentials import get_all_credentials, set_credentials

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure Mastodon as a sync destination.

You will be asked for:
  1. Instance URL — your server's base URL (e.g. https://mastodon.social)
  2. Access Token — from your instance:
       Preferences → Development → Your application → Access token
       (create an app with read + write scopes if you don't have one yet)

Both are stored in the local SQLite database.
"""


async def _get_credentials(engine: Engine) -> tuple[str, str]:
    creds = get_all_credentials(engine, NETWORK_MASTODON)
    instance_url = creds.get("instance_url", "").rstrip("/")
    access_token = creds.get("access_token")
    if not instance_url or not access_token:
        raise RuntimeError(
            "Mastodon not configured. Run: python main.py --auth=mastodon"
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


async def authenticate(engine: Engine) -> None:
    """Prompt for instance URL and access token, validate, store in database."""
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
        account = response.json()

    host = urlparse(instance_url).hostname or instance_url
    set_credentials(
        engine,
        NETWORK_MASTODON,
        {
            "instance_url": instance_url,
            "access_token": access_token,
            "username": account["username"],
        },
    )
    print(f"Mastodon configured for @{account['username']}@{host}")


async def fetch_posts(
    engine: Engine,
    since=None,
    include_replies: bool = True,
) -> list[Post]:
    raise NotImplementedError("Mastodon is configured as a destination network only")


async def download_media(media: MediaItem, access_token: str = "") -> bytes:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        response.raise_for_status()
        return response.content


async def publish_outbound(
    engine: Engine,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
) -> str:
    """Publish one outbound post to Mastodon. Returns status ID."""
    instance_url, access_token = await _get_credentials(engine)
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
            logger.info("Uploaded %s media %d (%d bytes)", log_id, i, len(raw))

        payload: dict[str, str | list[str]] = {"status": text or ""}
        if media_ids:
            payload["media_ids"] = media_ids

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
        logger.info("Posted %s as status %s", log_id, status_id)
        return str(status_id)


async def publish_post(
    engine: Engine,
    post: Post,
    media_bytes: list[bytes] | None = None,
) -> str:
    """Publish a single source post (wraps publish_outbound)."""
    outbound = OutboundPost(
        text=post.text,
        media=post.media,
        source_post_ids=[post.id],
    )
    return await publish_outbound(engine, outbound, media_bytes)
