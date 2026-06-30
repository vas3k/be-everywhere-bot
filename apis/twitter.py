import base64
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from utils.http_utils import format_api_error, parse_error_detail, twitter_api_error_extra
from utils.text_utils import strip_trailing_patterns
from apis.types import MediaItem, OutboundPost, Post, PublishResult
from utils.posts import sort_chronologically
from config import NETWORK_TWITTER, TWITTER_APP
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credentials,
)

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure X (Twitter) account for mesh sync.

You will be asked for:
  1. Bearer Token — from https://developer.x.com/en/portal/dashboard
       → your Project → App → "Keys and tokens" tab → Bearer Token → Generate / Regenerate
  2. Username — your @handle without the @ (e.g. "vas3k")

Credentials are stored per account label in the local SQLite database.

Note: X API v2 is pay-per-use. Reading your own timeline is an "owned read"
(~$0.001 per post). Watch mode only polls recent posts to keep costs low.
Use --since for one-off backfills.
"""

TWEET_FIELDS = (
    "created_at,conversation_id,referenced_tweets,attachments,text,in_reply_to_user_id"
)
MEDIA_FIELDS = "url,type,variants,alt_text,preview_image_url"
EXPANSIONS = "attachments.media_keys"
MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"

_TRAILING_TCO = re.compile(r"\s+https?://t\.co/\w+\s*$", re.IGNORECASE)
_TRAILING_STATUS_URL = re.compile(
    r"\s+https?://(?:twitter\.com|x\.com)/\S+\s*$", re.IGNORECASE
)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _best_video_url(media: dict[str, Any]) -> str | None:
    variants = media.get("variants") or []
    mp4s = [
        v for v in variants if v.get("url") and "mp4" in v.get("content_type", "")
    ]
    if not mp4s:
        return None
    mp4s.sort(key=lambda v: v.get("bit_rate") or 0, reverse=True)
    return mp4s[0]["url"]


def _media_url(media: dict[str, Any]) -> str | None:
    media_type = media.get("type", "photo")
    if media_type == "photo":
        return media.get("url")
    if media_type in ("video", "animated_gif"):
        return _best_video_url(media) or media.get("preview_image_url")
    return media.get("url")


def _extract_media(tweet: dict[str, Any], includes: dict[str, Any]) -> list[MediaItem]:
    media_keys = (tweet.get("attachments") or {}).get("media_keys") or []
    media_lookup = {m["media_key"]: m for m in includes.get("media") or []}
    items: list[MediaItem] = []

    for key in media_keys:
        media = media_lookup.get(key)
        if not media:
            logger.warning("Tweet %s: media key %s not in includes", tweet.get("id"), key)
            continue
        media_type = media.get("type", "photo")
        url = _media_url(media)
        if not url:
            logger.warning("Tweet %s: no URL for media type %s", tweet.get("id"), media_type)
            continue
        items.append(
            MediaItem(
                url=url,
                media_type=media_type,
                alt_text=media.get("alt_text"),
            )
        )
    return items


def _strip_trailing_links(text: str, *, has_media: bool) -> str:
    patterns = [_TRAILING_STATUS_URL]
    if has_media:
        patterns.append(_TRAILING_TCO)
    return strip_trailing_patterns(text, patterns)


def _skip_reason(tweet: dict[str, Any]) -> str | None:
    if tweet.get("text", "").lstrip().startswith("@"):
        return "at_reply"
    for ref in tweet.get("referenced_tweets") or []:
        ref_type = ref.get("type")
        if ref_type == "retweeted":
            return "retweet"
        if ref_type == "quoted":
            return "quote"
    if tweet.get("text", "").startswith("RT @"):
        return "retweet"
    return None


def _tweet_to_post(tweet: dict[str, Any], includes: dict[str, Any], author_id: str) -> Post:
    in_reply_to_id = None
    for ref in tweet.get("referenced_tweets") or []:
        if ref.get("type") == "replied_to":
            in_reply_to_id = ref.get("id")
            break

    conversation_id = tweet.get("conversation_id") or tweet["id"]
    in_reply_to_user_id = tweet.get("in_reply_to_user_id")
    media = _extract_media(tweet, includes)
    text = _strip_trailing_links(tweet.get("text", ""), has_media=bool(media))

    return Post(
        id=str(tweet["id"]),
        text=text,
        created_at=_parse_datetime(tweet["created_at"]),
        conversation_id=str(conversation_id),
        author_id=str(author_id),
        media=media,
        in_reply_to_id=str(in_reply_to_id) if in_reply_to_id else None,
        in_reply_to_user_id=str(in_reply_to_user_id) if in_reply_to_user_id else None,
        is_thread_root=str(conversation_id) == str(tweet["id"]),
    )


def _require_creds(engine: Engine, account_id: int) -> dict[str, str]:
    creds = get_all_credentials(engine, account_id)
    missing = [k for k in ("bearer_token", "user_id", "username") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"X account {account_id} not configured (missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=twitter"
        )
    return creds


async def _api_get(
    bearer_token: str,
    path: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{TWITTER_APP.api_base_url}{path}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, headers=headers, params=params)
        if not response.is_success:
            detail = parse_error_detail(response)
            raise RuntimeError(
                format_api_error(
                    "X",
                    response.status_code,
                    detail,
                    extra=twitter_api_error_extra,
                )
            )
        return response.json()


async def _api_post(bearer_token: str, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    url = f"{TWITTER_APP.api_base_url}{path}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, headers=headers, json=json_body)
        if not response.is_success:
            detail = parse_error_detail(response)
            raise RuntimeError(
                format_api_error(
                    "X",
                    response.status_code,
                    detail,
                    extra=twitter_api_error_extra,
                )
            )
        return response.json()


async def _lookup_user(bearer_token: str, username: str) -> dict[str, Any]:
    username = username.lstrip("@")
    data = await _api_get(
        bearer_token,
        f"/users/by/username/{username}",
        params={"user.fields": "username"},
    )
    user = data.get("data")
    if not user:
        raise RuntimeError(f"User @{username} not found")
    return user


def _twitter_media_type(item: MediaItem) -> tuple[str, str]:
    if item.media_type == "photo":
        return "image/jpeg", "tweet_image"
    if item.media_type == "animated_gif":
        return "video/mp4", "tweet_gif"
    return "video/mp4", "tweet_video"


async def _upload_media(
    client: httpx.AsyncClient,
    bearer_token: str,
    raw: bytes,
    item: MediaItem,
) -> str:
    content_type, category = _twitter_media_type(item)
    headers = {"Authorization": f"Bearer {bearer_token}"}

    init = await client.post(
        MEDIA_UPLOAD_URL,
        headers=headers,
        data={
            "command": "INIT",
            "total_bytes": len(raw),
            "media_type": content_type,
            "media_category": category,
        },
    )
    init.raise_for_status()
    media_id = init.json()["media_id_string"]

    append = await client.post(
        MEDIA_UPLOAD_URL,
        headers=headers,
        data={
            "command": "APPEND",
            "media_id": media_id,
            "segment_index": 0,
            "media_data": base64.b64encode(raw).decode("ascii"),
        },
    )
    append.raise_for_status()

    finalize = await client.post(
        MEDIA_UPLOAD_URL,
        headers=headers,
        data={"command": "FINALIZE", "media_id": media_id},
    )
    finalize.raise_for_status()
    return str(media_id)


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    bearer_token = input("Bearer Token: ").strip()
    username = input("Username (without @): ").strip().lstrip("@")

    if not bearer_token or not username:
        raise RuntimeError("Bearer token and username are required.")

    user = await _lookup_user(bearer_token, username)
    creds = {
        "bearer_token": bearer_token,
        "user_id": str(user["id"]),
        "username": user["username"],
    }

    existing = find_account(engine, NETWORK_TWITTER, label)
    if existing:
        set_credentials(engine, existing.id, creds)
        print(f"X account '{label}' updated for @{user['username']}")
        return existing

    account = create_account(engine, NETWORK_TWITTER, label, str(user["id"]))
    set_credentials(engine, account.id, creds)
    print(f"X account '{label}' configured for @{user['username']}")
    return account


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    creds = _require_creds(engine, account_id)
    bearer_token = creds["bearer_token"]
    user_id = creds["user_id"]

    base_params: dict[str, str] = {
        "max_results": "100",
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
        "media.fields": MEDIA_FIELDS,
    }
    since_utc = since.astimezone(timezone.utc) if since else None
    if since_utc:
        base_params["start_time"] = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    base_params["exclude"] = "retweets"
    if not include_replies:
        base_params["exclude"] = "replies,retweets"

    all_posts: list[Post] = []
    skipped: dict[str, int] = {"retweet": 0, "quote": 0, "at_reply": 0}
    raw_count = 0
    page = 0
    pagination_token: str | None = None

    while True:
        page += 1
        params = dict(base_params)
        if pagination_token:
            params["pagination_token"] = pagination_token

        data = await _api_get(
            bearer_token,
            f"/users/{user_id}/tweets",
            params=params,
        )

        tweets = data.get("data") or []
        includes = data.get("includes") or {}
        raw_count += len(tweets)
        reached_since_boundary = False

        for tweet in tweets:
            if since_utc:
                created_at = _parse_datetime(tweet["created_at"])
                if created_at < since_utc:
                    reached_since_boundary = True
                    continue
            reason = _skip_reason(tweet)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            all_posts.append(_tweet_to_post(tweet, includes, user_id))

        meta = data.get("meta") or {}
        pagination_token = meta.get("next_token")
        if since_utc and reached_since_boundary:
            break
        if max_pages is not None and page >= max_pages:
            break
        if not pagination_token:
            break

    return sort_chronologically(all_posts)


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    creds = _require_creds(engine, account_id)
    bearer_token = creds["bearer_token"]
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        if response.status_code in (401, 403) and bearer_token:
            response = await client.get(
                media.url,
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
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
    creds = _require_creds(engine, account_id)
    bearer_token = creds["bearer_token"]
    text = outbound.text or ""
    media = outbound.media
    bytes_list = media_bytes or []

    async with httpx.AsyncClient(timeout=120.0) as client:
        media_ids: list[str] = []
        for item, raw in zip(media, bytes_list):
            media_ids.append(await _upload_media(client, bearer_token, raw, item))

        payload: dict[str, Any] = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}

        data = await _api_post(bearer_token, "/tweets", payload)
        tweet_id = data.get("data", {}).get("id")
        if not tweet_id:
            raise RuntimeError(f"X post tweet: missing id in response: {data}")
        post_id = str(tweet_id)
        return PublishResult(post_id=post_id, reply_ref=post_id)
