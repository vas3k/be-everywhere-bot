import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from utils.http_utils import format_api_error, parse_error_detail
from utils.meta_tokens import (
    FACEBOOK_TOKEN,
    INSTAGRAM_TOKEN,
    MetaTokenConfig,
    auth_error_hint,
    describe_expiry,
    ensure_fresh_token,
    initialize_token,
    token_credentials,
)
from apis.types import MediaItem, OutboundPost, Post, PublishResult
from utils.posts import sort_chronologically
from config import INSTAGRAM_APP, NETWORK_INSTAGRAM, POST_MIN_AGE_MINUTES, NetworkLimits
from db.accounts import (
    Account,
    save_account,
    get_all_credentials,
    set_credential,
    update_remote_id,
)

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure Instagram as a one-way sync source.

You will be asked for:
  1. Access Token — from https://developers.facebook.com/apps/
       → Instagram product → API setup with Instagram login
       → generate a User access token with instagram_business_basic
  2. (optional) App Secret — App settings → Basic.
       Only needed to upgrade a short-lived (1 hour) token to a 60-day one.
  3. (optional) Username — your @handle; looked up automatically if omitted

Requires an Instagram Business or Creator account. Posts and active stories
(24 h window) are republished to your other networks. Instagram is never
used as a destination.

Tokens are long-lived (60 days) and renewed automatically before they expire.

Note: Instagram media URLs expire — the bot downloads media at publish time.
"""

MEDIA_FIELDS = (
    "id,caption,media_type,media_url,thumbnail_url,timestamp,permalink,"
    "children{media_type,media_url,thumbnail_url}"
)
STORY_FIELDS = "id,caption,media_type,media_url,thumbnail_url,timestamp"

STORY_ID_PREFIX = "story_"
POST_ID_PREFIX = "post_"
STORY_GROUP_PREFIX = "story_group_"

# Which login the stored token came from — they renew through different endpoints.
TOKEN_KIND_INSTAGRAM = "instagram"
TOKEN_KIND_FACEBOOK = "facebook"


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00").replace("+0000", "+00:00")
    return datetime.fromisoformat(normalized)


def _instagram_media_type(media_type: str) -> str:
    if media_type in ("VIDEO", "REELS"):
        return "video"
    return "photo"


def _extract_media(item: dict[str, Any]) -> list[MediaItem]:
    media_type = item.get("media_type", "IMAGE")
    if media_type == "CAROUSEL_ALBUM":
        items: list[MediaItem] = []
        for child in item.get("children", {}).get("data", []):
            items.extend(_extract_media(child))
        return items

    if media_type in ("IMAGE", "VIDEO", "REELS"):
        url = item.get("media_url") or item.get("thumbnail_url")
        if not url:
            return []
        return [
            MediaItem(
                url=url,
                media_type=_instagram_media_type(media_type),
                alt_text=item.get("alt_text"),
            )
        ]

    return []


def _item_caption(item: dict[str, Any]) -> str:
    return (item.get("caption") or "").strip()


def _feed_item_to_post(item: dict[str, Any], author_id: str) -> Post | None:
    media = _extract_media(item)
    text = _item_caption(item)
    if not text and not media:
        return None

    post_id = f"{POST_ID_PREFIX}{item['id']}"
    return Post(
        id=post_id,
        text=text,
        created_at=_parse_timestamp(item["timestamp"]),
        conversation_id=post_id,
        author_id=author_id,
        media=media,
        is_thread_root=True,
    )


def _story_item_to_post(item: dict[str, Any], author_id: str) -> Post | None:
    media = _extract_media(item)
    if not media:
        return None

    story_id = f"{STORY_ID_PREFIX}{item['id']}"
    return Post(
        id=story_id,
        text=_item_caption(item),
        created_at=_parse_timestamp(item["timestamp"]),
        conversation_id=story_id,
        author_id=author_id,
        media=media,
        is_thread_root=True,
    )


def _assign_story_groups(stories: list[Post], window_minutes: int) -> list[Post]:
    """Group consecutive stories posted within window_minutes of each other."""
    if not stories:
        return []

    window = timedelta(minutes=window_minutes)
    ordered = sort_chronologically(stories)
    clusters: list[list[Post]] = [[ordered[0]]]

    for story in ordered[1:]:
        if story.created_at - clusters[-1][-1].created_at <= window:
            clusters[-1].append(story)
        else:
            clusters.append([story])

    grouped: list[Post] = []
    for cluster in clusters:
        conv_id = f"{STORY_GROUP_PREFIX}{cluster[0].id}"
        for story in cluster:
            grouped.append(
                Post(
                    id=story.id,
                    text=story.text,
                    created_at=story.created_at,
                    conversation_id=conv_id,
                    author_id=story.author_id,
                    media=story.media,
                    is_thread_root=story.id == cluster[0].id,
                )
            )
    return grouped


def is_story_batch(batch: list[Post]) -> bool:
    return bool(batch) and all(post.id.startswith(STORY_ID_PREFIX) for post in batch)


def build_outbounds(batch: list[Post], limits: NetworkLimits) -> list[OutboundPost]:
    from utils.thread_processor import build_outbound_posts

    if is_story_batch(batch):
        return build_story_outbounds(batch, limits)
    return build_outbound_posts(batch, limits)


def build_story_outbounds(batch: list[Post], limits: NetworkLimits) -> list[OutboundPost]:
    from utils.thread_processor import build_outbound_posts

    ordered = sort_chronologically(batch)
    if not ordered:
        return []
    if limits.max_media_group == 1:
        return [outbound for post in ordered for outbound in build_outbound_posts([post], limits)]

    caption = next((post.text for post in ordered if post.text.strip()), "")
    slides = [replace(post, text=caption if index == 0 else "") for index, post in enumerate(ordered)]
    return build_outbound_posts(slides, limits)


def _require_creds(engine: Engine, account_id: int) -> dict[str, str]:
    creds = get_all_credentials(engine, account_id)
    missing = [k for k in ("access_token", "user_id") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"Instagram account {account_id} not configured (missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=instagram"
        )
    return creds


async def _api_get(
    base_url: str,
    access_token: str,
    path: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    query = dict(params or {})
    query["access_token"] = access_token
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, params=query)
        if not response.is_success:
            detail = parse_error_detail(response)
            raise RuntimeError(
                format_api_error(
                    "Instagram",
                    response.status_code,
                    detail,
                    extra=auth_error_hint(INSTAGRAM_TOKEN),
                )
            )
        return response.json()


async def _lookup_profile(access_token: str) -> dict[str, str]:
    try:
        data = await _api_get(
            INSTAGRAM_APP.api_base_url,
            access_token,
            "me",
            {"fields": "user_id,username"},
        )
        user_id = data.get("user_id") or data.get("id")
        if user_id:
            return {
                "user_id": str(user_id),
                "username": data.get("username", ""),
                "token_kind": TOKEN_KIND_INSTAGRAM,
            }
    except RuntimeError:
        pass

    pages = await _api_get(
        INSTAGRAM_APP.facebook_graph_url,
        access_token,
        "me/accounts",
        {"fields": "instagram_business_account{id,username}"},
    )
    for page in pages.get("data") or []:
        ig_account = page.get("instagram_business_account") or {}
        if ig_account.get("id"):
            return {
                "user_id": str(ig_account["id"]),
                "username": ig_account.get("username", ""),
                "token_kind": TOKEN_KIND_FACEBOOK,
            }

    raise RuntimeError(
        "No Instagram Business or Creator account found for this token. "
        "Check app permissions (instagram_business_basic) and account type."
    )


def _token_config(token_kind: str | None) -> MetaTokenConfig:
    return FACEBOOK_TOKEN if token_kind == TOKEN_KIND_FACEBOOK else INSTAGRAM_TOKEN


async def _ensure_session(engine: Engine, account_id: int) -> dict[str, str]:
    """Credentials with a valid (auto-renewed) token and a resolved profile."""
    creds = _require_creds(engine, account_id)
    access_token = await ensure_fresh_token(
        engine, account_id, creds, _token_config(creds.get("token_kind"))
    )
    creds = {**creds, "access_token": access_token}
    if creds.get("username"):
        return creds

    profile = await _lookup_profile(access_token)
    creds = {**creds, **profile}
    set_credential(engine, account_id, "user_id", creds["user_id"])
    set_credential(engine, account_id, "token_kind", creds["token_kind"])
    if creds["username"]:
        set_credential(engine, account_id, "username", creds["username"])
    update_remote_id(engine, account_id, creds["user_id"])
    return creds


async def authenticate(engine: Engine, label: str = "default", *, project_id: int = 1, sync_role: str | None = None, credentials: dict[str, str] | None = None) -> Account:
    report = print if credentials is None else logger.info
    if credentials is None:
        print(AUTH_HELP)
    access_token = (credentials.get("access_token", "") if credentials is not None else input("Access Token: ")).strip()
    if not access_token:
        raise RuntimeError("Access token is required.")

    profile = await _lookup_profile(access_token)
    token_config = _token_config(profile["token_kind"])

    app_creds: dict[str, str] = {}
    if token_config is FACEBOOK_TOKEN:
        # Facebook-login tokens can only be renewed with full app credentials.
        app_id = (credentials.get("app_id", "") if credentials is not None else input("Facebook App ID: ")).strip()
        if app_id:
            app_creds["app_id"] = app_id
    app_secret = (credentials.get("app_secret", "") if credentials is not None else input("App Secret (optional, press Enter to skip): ")).strip()
    if app_secret:
        app_creds["app_secret"] = app_secret
    username = (credentials.get("username", "") if credentials is not None else input("Username (optional, press Enter to auto-detect): ")).strip().lstrip("@")

    token = await initialize_token(token_config, access_token, app_creds)
    creds = {
        **app_creds,
        **token_credentials(token),
        "user_id": profile["user_id"],
        "username": username or profile.get("username", ""),
        "token_kind": profile["token_kind"],
    }

    account = save_account(engine, NETWORK_INSTAGRAM, label, creds["user_id"], creds, project_id=project_id, sync_role=sync_role)
    report(f"Configured {account.network}/{account.label} — token {describe_expiry(token)}")
    if not token.renewed:
        report("Token was not upgraded. Supply the App Secret to exchange a short-lived token.")
    return account


async def _fetch_paged(
    access_token: str,
    user_id: str,
    endpoint: str,
    fields: str,
    *,
    since: datetime | None,
    max_pages: int | None,
) -> list[dict[str, Any]]:
    since_utc = since.astimezone(timezone.utc) if since else None
    params: dict[str, str] = {"fields": fields, "limit": "50"}
    items: list[dict[str, Any]] = []
    after: str | None = None
    page = 0

    while True:
        page += 1
        page_params = dict(params)
        if after:
            page_params["after"] = after

        data = await _api_get(
            INSTAGRAM_APP.api_base_url,
            access_token,
            f"{user_id}/{endpoint}",
            page_params,
        )
        batch = data.get("data") or []
        reached_since = False

        for item in batch:
            timestamp = item.get("timestamp")
            if not timestamp:
                continue
            created_at = _parse_timestamp(timestamp)
            if since_utc and created_at < since_utc:
                reached_since = True
                continue
            items.append(item)

        paging = data.get("paging", {})
        after = paging.get("cursors", {}).get("after")
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
    creds = await _ensure_session(engine, account_id)
    access_token = creds["access_token"]
    user_id = creds["user_id"]

    feed_items = await _fetch_paged(
        access_token,
        user_id,
        "media",
        MEDIA_FIELDS,
        since=since,
        max_pages=max_pages,
    )
    story_items = await _fetch_paged(
        access_token,
        user_id,
        "stories",
        STORY_FIELDS,
        since=since,
        max_pages=max_pages,
    )

    posts: list[Post] = []
    for item in feed_items:
        post = _feed_item_to_post(item, user_id)
        if post:
            posts.append(post)

    stories: list[Post] = []
    for item in story_items:
        story = _story_item_to_post(item, user_id)
        if story:
            stories.append(story)

    if stories:
        stories = _assign_story_groups(stories, POST_MIN_AGE_MINUTES)
        logger.info(
            "Instagram stories: %d slide(s) in %d group(s)",
            len(stories),
            len({s.conversation_id for s in stories}),
        )

    posts.extend(stories)
    posts = sort_chronologically(posts)
    logger.info(
        "Instagram @%s: %d feed post(s), %d story slide(s)",
        creds.get("username") or user_id,
        len(feed_items),
        len(story_items),
    )
    return posts


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        if response.status_code == 403:
            logger.warning(
                "Instagram media URL expired or forbidden — re-fetch may be needed"
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
    raise NotImplementedError("Instagram is configured as a source-only network")
