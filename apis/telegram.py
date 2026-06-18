import asyncio
import json
import logging
import re

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post
from config import NETWORK_TELEGRAM, TELEGRAM_APP
from db.credentials import get_all_credentials, set_credential

logger = logging.getLogger(__name__)

_RETRY_AFTER_RE = re.compile(r"retry after (\d+)", re.IGNORECASE)


async def _get_bot_credentials(engine: Engine) -> tuple[str, str]:
    creds = get_all_credentials(engine, NETWORK_TELEGRAM)
    bot_token = creds.get("bot_token")
    channel_id = creds.get("channel_id")
    if not bot_token or not channel_id:
        raise RuntimeError(
            "Telegram not configured. Run: python main.py --auth=telegram"
        )
    return bot_token, channel_id


def _api_url(bot_token: str, method: str) -> str:
    return f"{TELEGRAM_APP.api_base}/bot{bot_token}/{method}"


def _telegram_api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        return body.get("description", response.text)
    except Exception:
        return response.text


def _retry_after_seconds(response: httpx.Response) -> int | None:
    try:
        body = response.json()
        params = body.get("parameters") or {}
        if "retry_after" in params:
            return int(params["retry_after"])
        match = _RETRY_AFTER_RE.search(body.get("description", ""))
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    while True:
        response = await client.post(url, **kwargs)
        if response.status_code != 429:
            return response
        wait = _retry_after_seconds(response) or 30
        logger.warning("Telegram rate limit — waiting %ds", wait)
        await asyncio.sleep(wait)


async def authenticate(engine: Engine) -> None:
    """Prompt for bot token and channel ID, store in database."""
    bot_token = input("Telegram bot token: ").strip()
    channel_id = input("Telegram channel ID (e.g. @mychannel or -1001234567890): ").strip()

    async with httpx.AsyncClient() as client:
        response = await client.get(_api_url(bot_token, "getMe"))
        response.raise_for_status()
        bot = response.json()["result"]

    set_credential(engine, NETWORK_TELEGRAM, "bot_token", bot_token)
    set_credential(engine, NETWORK_TELEGRAM, "channel_id", channel_id)
    print(f"Telegram configured for bot @{bot['username']} -> {channel_id}")


async def fetch_posts(
    engine: Engine,
    since=None,
    include_replies: bool = True,
) -> list[Post]:
    raise NotImplementedError("Telegram is configured as a destination network only")


async def download_media(media: MediaItem, access_token: str = "") -> bytes:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        response.raise_for_status()
        return response.content


def _media_filename(item: MediaItem, index: int) -> str:
    if item.media_type == "photo":
        return f"photo{index}.jpg"
    if item.media_type == "animated_gif":
        return f"animation{index}.mp4"
    return f"video{index}.mp4"


def _send_method(item: MediaItem) -> str:
    if item.media_type == "photo":
        return "sendPhoto"
    if item.media_type == "animated_gif":
        return "sendAnimation"
    return "sendVideo"


def _file_field(item: MediaItem) -> str:
    if item.media_type == "photo":
        return "photo"
    if item.media_type == "animated_gif":
        return "animation"
    return "video"


async def publish_outbound(
    engine: Engine,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
) -> str:
    """Publish one outbound post to Telegram. Returns message ID."""
    bot_token, channel_id = await _get_bot_credentials(engine)
    text = outbound.text or None
    media = outbound.media
    bytes_list = media_bytes or []
    log_id = outbound.source_post_ids[0] if outbound.source_post_ids else "?"

    if media and len(bytes_list) != len(media):
        raise RuntimeError(
            f"Media upload mismatch for post {log_id}: "
            f"{len(media)} attachment(s) but {len(bytes_list)} downloaded"
        )

    async with httpx.AsyncClient(timeout=120.0) as client:
        if not media:
            response = await _post_with_retry(
                client,
                _api_url(bot_token, "sendMessage"),
                json={"chat_id": channel_id, "text": text or "(empty)"},
            )
            if not response.is_success:
                raise RuntimeError(f"Telegram sendMessage: {_telegram_api_error(response)}")
            return str(response.json()["result"]["message_id"])

        if len(media) == 1:
            item = media[0]
            data: dict[str, str] = {"chat_id": channel_id}
            if text:
                data["caption"] = text

            field = _file_field(item)
            method = _send_method(item)
            files = {field: (_media_filename(item, 0), bytes_list[0])}
            response = await _post_with_retry(
                client,
                _api_url(bot_token, method),
                data=data,
                files=files,
            )
            if not response.is_success:
                raise RuntimeError(f"Telegram {method}: {_telegram_api_error(response)}")
            logger.info("Sent %s via %s (%d bytes)", log_id, method, len(bytes_list[0]))
            return str(response.json()["result"]["message_id"])

        media_group = []
        files = {}
        for i, (item, raw) in enumerate(zip(media, bytes_list)):
            attach_name = f"file{i}"
            tg_type = "photo" if item.media_type == "photo" else "video"
            media_group.append({"type": tg_type, "media": f"attach://{attach_name}"})
            files[attach_name] = (_media_filename(item, i), raw)

        if text:
            media_group[0]["caption"] = text

        response = await _post_with_retry(
            client,
            _api_url(bot_token, "sendMediaGroup"),
            data={"chat_id": channel_id, "media": json.dumps(media_group)},
            files=files,
        )
        if not response.is_success:
            raise RuntimeError(f"Telegram sendMediaGroup: {_telegram_api_error(response)}")
        messages = response.json()["result"]
        logger.info("Sent %s as media group (%d items)", log_id, len(media))
        return str(messages[0]["message_id"])


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
