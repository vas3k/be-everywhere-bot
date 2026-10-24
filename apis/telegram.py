import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, or_, update
from sqlalchemy.engine import Connection, Engine

from utils.posts import sort_chronologically
from apis.types import MediaItem, OutboundPost, Post, PublishResult
from config import NETWORK_TELEGRAM, TELEGRAM_APP
from db.accounts import (
    Account,
    save_account,
    get_account,
    get_all_credentials,
    find_account,
    list_accounts,
)

from db.helpers import write_credentials
from db.posts import save_posts
from db.schema import accounts

logger = logging.getLogger(__name__)

_RETRY_AFTER_RE = re.compile(r"retry after (\d+)", re.IGNORECASE)
TG_FILE_PREFIX = "tgfile:"


async def _get_bot_credentials(engine: Engine, account_id: int) -> tuple[str, str]:
    creds = get_all_credentials(engine, account_id)
    bot_token = creds.get("bot_token")
    channel_id = creds.get("channel_id")
    if not bot_token or not channel_id:
        raise RuntimeError(
            f"Telegram account {account_id} not configured. "
            "Run: python main.py --auth=telegram"
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
    for attempt in range(3):
        response = await client.post(url, **kwargs)
        if response.status_code != 429 or attempt == 2:
            return response
        wait = _retry_after_seconds(response) or 30
        logger.warning("Telegram rate limit — waiting %ds", wait)
        await asyncio.sleep(wait)


def _normalize_chat_id(channel_id: str) -> str:
    return channel_id.strip().lower()


def _chat_matches(channel_id: str, chat: dict) -> bool:
    configured = _normalize_chat_id(channel_id)
    chat_username = (chat.get("username") or "").lower()
    chat_id = str(chat.get("id", ""))
    if configured.startswith("@"):
        return chat_username == configured.lstrip("@")
    return chat_id == configured


def _extract_media(message: dict) -> list[MediaItem]:
    items: list[MediaItem] = []
    if photos := message.get("photo"):
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        items.append(
            MediaItem(
                url=f"{TG_FILE_PREFIX}{largest['file_id']}",
                media_type="photo",
            )
        )
    elif video := message.get("video"):
        items.append(
            MediaItem(
                url=f"{TG_FILE_PREFIX}{video['file_id']}",
                media_type="video",
            )
        )
    elif animation := message.get("animation"):
        items.append(
            MediaItem(
                url=f"{TG_FILE_PREFIX}{animation['file_id']}",
                media_type="animated_gif",
            )
        )
    return items


def _apply_reply_to(data: dict[str, str], reply_to: str | None) -> None:
    if reply_to:
        data["reply_to_message_id"] = reply_to


def _media_group_type(item: MediaItem) -> str:
    return "photo" if item.media_type == "photo" else "video"


def _message_to_post(message: dict, channel_id: str) -> Post | None:
    chat = message.get("chat") or {}
    if not _chat_matches(channel_id, chat):
        return None

    text = message.get("text") or message.get("caption") or ""
    media = _extract_media(message)
    if not text and not media:
        return None

    message_id = str(message["message_id"])
    conversation_id = (
        str(message["media_group_id"]) if message.get("media_group_id") else message_id
    )
    created_at = datetime.fromtimestamp(message["date"], tz=timezone.utc)

    return Post(
        id=message_id,
        text=text,
        created_at=created_at,
        conversation_id=conversation_id,
        author_id=str(chat.get("id", channel_id)),
        media=media,
        is_thread_root=conversation_id == message_id,
    )


def record_chat_identity(connection: Connection, account_id: int, chat: dict) -> None:
    if not chat.get("id"):
        return
    remote_id = str(chat["id"])
    aliases = [remote_id]
    if chat.get("username"):
        aliases.append("@" + chat["username"].lower())
    connection.execute(update(accounts).where(
        accounts.c.network == NETWORK_TELEGRAM,
        or_(accounts.c.id == account_id, func.lower(accounts.c.remote_id).in_(aliases)),
    ).values(remote_id=remote_id))


async def authenticate(engine: Engine, label: str = "default", *, project_id: int = 1, sync_role: str | None = None, credentials: dict[str, str] | None = None) -> Account:
    report = print if credentials is None else logger.info
    bot_token = (credentials.get("bot_token", "") if credentials is not None else input("Telegram bot token: ")).strip()
    channel_id = (credentials.get("channel_id", "") if credentials is not None else input("Telegram channel ID (e.g. @mychannel or -1001234567890): ")).strip()

    if not bot_token or not channel_id:
        raise ValueError("Bot token and channel ID are required")

    async with httpx.AsyncClient() as client:
        response = await client.get(_api_url(bot_token, "getMe"))
        response.raise_for_status()
        if not response.json().get("result", {}).get("id"):
            raise ValueError("Telegram returned no bot identity")
        response = await client.get(_api_url(bot_token, "getChat"), params={"chat_id": channel_id})
        response.raise_for_status()
        chat = response.json().get("result") or {}
        if not chat.get("id"):
            raise ValueError("Telegram returned no channel identity")

    existing = find_account(engine, NETWORK_TELEGRAM, label, project_id=project_id)
    previous = get_all_credentials(engine, existing.id) if existing else {}
    offset = previous.get("update_offset", "0") if previous.get("bot_token") == bot_token else "0"
    creds = {"bot_token": bot_token, "channel_id": channel_id, "update_offset": offset}

    account = save_account(engine, NETWORK_TELEGRAM, label, str(chat["id"]), creds, project_id=project_id, sync_role=sync_role)
    with engine.begin() as connection:
        record_chat_identity(connection, account.id, chat)
    report(f"Configured {account.network}/{account.label}")
    return account


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    if get_account(engine, account_id).sync_role == "destination_only":
        return []
    bot_token, channel_id = await _get_bot_credentials(engine, account_id)
    peers = {}
    source_ids = set()
    for account in list_accounts(engine, NETWORK_TELEGRAM):
        credentials = get_all_credentials(engine, account.id)
        if credentials.get("bot_token") == bot_token:
            peers[account.id] = credentials
            if account.sync_role in ("source_only", "source_and_destination"):
                source_ids.add(account.id)
    offset = max(int(credentials.get("update_offset") or 0) for credentials in peers.values())
    posts = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            response = await client.get(
                _api_url(bot_token, "getUpdates"),
                params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["channel_post"])},
            )
            response.raise_for_status()
            updates = response.json().get("result") or []
            if not updates:
                break

            offset = max(update["update_id"] for update in updates) + 1
            # A bot has one update queue for every channel. Save all configured channels
            # before the next getUpdates request acknowledges these updates remotely.
            with engine.begin() as connection:
                for peer_id, credentials in peers.items():
                    incoming = []
                    for update in updates:
                        message = update.get("channel_post")
                        if message:
                            post = _message_to_post(message, credentials["channel_id"])
                            if post:
                                record_chat_identity(connection, peer_id, message.get("chat") or {})
                                if peer_id in source_ids:
                                    incoming.append(post)
                    save_posts(connection, peer_id, incoming)
                    write_credentials(connection, peer_id, {"update_offset": str(offset)})
                    if peer_id == account_id:
                        posts.extend(incoming)

    if since:
        posts = [post for post in posts if post.created_at >= since]
    return sort_chronologically(posts)


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    if not media.url.startswith(TG_FILE_PREFIX):
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            response = await client.get(media.url)
            response.raise_for_status()
            return response.content

    bot_token, _ = await _get_bot_credentials(engine, account_id)
    file_id = media.url.removeprefix(TG_FILE_PREFIX)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(
            _api_url(bot_token, "getFile"),
            params={"file_id": file_id},
        )
        response.raise_for_status()
        file_path = response.json()["result"]["file_path"]
        file_response = await client.get(
            f"{TELEGRAM_APP.api_base}/file/bot{bot_token}/{file_path}"
        )
        file_response.raise_for_status()
        return file_response.content


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
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    bot_token, channel_id = await _get_bot_credentials(engine, account_id)
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
            payload: dict[str, str] = {"chat_id": channel_id, "text": text or " "}
            _apply_reply_to(payload, reply_to)
            response = await _post_with_retry(
                client,
                _api_url(bot_token, "sendMessage"),
                json=payload,
            )
            if not response.is_success:
                raise RuntimeError(f"Telegram sendMessage: {_telegram_api_error(response)}")
            message = response.json()["result"]
            with engine.begin() as connection:
                record_chat_identity(connection, account_id, message.get("chat") or {})
            post_id = str(message["message_id"])
            return PublishResult(post_id=post_id, reply_ref=post_id)

        if len(media) == 1:
            item = media[0]
            data: dict[str, str] = {"chat_id": channel_id}
            if text:
                data["caption"] = text
            _apply_reply_to(data, reply_to)
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
            message = response.json()["result"]
            with engine.begin() as connection:
                record_chat_identity(connection, account_id, message.get("chat") or {})
            post_id = str(message["message_id"])
            return PublishResult(post_id=post_id, reply_ref=post_id)

        media_group = []
        files = {}
        for i, (item, raw) in enumerate(zip(media, bytes_list)):
            attach_name = f"file{i}"
            media_group.append(
                {"type": _media_group_type(item), "media": f"attach://{attach_name}"}
            )
            files[attach_name] = (_media_filename(item, i), raw)

        if text:
            media_group[0]["caption"] = text

        group_data: dict[str, str] = {
            "chat_id": channel_id,
            "media": json.dumps(media_group),
        }
        _apply_reply_to(group_data, reply_to)
        response = await _post_with_retry(
            client,
            _api_url(bot_token, "sendMediaGroup"),
            data=group_data,
            files=files,
        )
        if not response.is_success:
            raise RuntimeError(f"Telegram sendMediaGroup: {_telegram_api_error(response)}")
        messages = response.json()["result"]
        with engine.begin() as connection:
            record_chat_identity(connection, account_id, messages[0].get("chat") or {})
        post_id = str(messages[0]["message_id"])
        return PublishResult(
            post_id=post_id, reply_ref=post_id,
            mirrored_ids=tuple(str(message["message_id"]) for message in messages),
        )
