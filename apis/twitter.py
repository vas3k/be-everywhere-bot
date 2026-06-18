import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, Post
from config import NETWORK_TWITTER, TWITTER_APP
from db.credentials import get_all_credentials, set_credentials

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure X (Twitter) as a sync source.

You will be asked for:
  1. Bearer Token — from https://developer.x.com/en/portal/dashboard
       → your Project → App → "Keys and tokens" tab → Bearer Token → Generate / Regenerate
  2. Username — your @handle without the @ (e.g. "vas3k")

Both are stored in the local SQLite database.

Note: X API v2 is pay-per-use. Reading your own timeline is an "owned read"
(~$0.001 per tweet). Watch mode only polls recent tweets to keep costs low.
Use --since for one-off backfills.
"""

TWEET_FIELDS = (
    "created_at,conversation_id,referenced_tweets,attachments,text,in_reply_to_user_id"
)
MEDIA_FIELDS = "url,type,variants,alt_text,preview_image_url"
EXPANSIONS = "attachments.media_keys"

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
    """Strip Twitter-appended trailing links only — keep user-shared URLs."""
    text = text.rstrip()
    text = _TRAILING_STATUS_URL.sub("", text).rstrip()
    # One trailing t.co with media = Twitter's auto-appended media/card link
    if has_media:
        text = _TRAILING_TCO.sub("", text).rstrip()
    return text


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


def filter_originals_and_threads(posts: list[Post]) -> list[Post]:
    """Keep standalone tweets and self-replies; drop replies to other people."""
    if not posts:
        return []

    author_id = posts[0].author_id
    own_ids = {p.id for p in posts}
    own_thread_roots = {p.id for p in posts if p.conversation_id == p.id}

    kept: list[Post] = []
    skipped = 0

    for post in posts:
        if post.in_reply_to_user_id is not None:
            if str(post.in_reply_to_user_id) != str(author_id):
                skipped += 1
                logger.debug(
                    "Skipping reply %s to user %s", post.id, post.in_reply_to_user_id
                )
                continue
        elif post.in_reply_to_id is not None:
            # Thread reply to own older tweet may fall outside this page — use conversation
            in_own_thread = (
                post.in_reply_to_id in own_ids
                or post.conversation_id in own_thread_roots
            )
            if not in_own_thread:
                skipped += 1
                logger.debug(
                    "Skipping reply %s in foreign conversation %s",
                    post.id,
                    post.conversation_id,
                )
                continue

        kept.append(post)

    if skipped:
        logger.info("Filtered out %d reply/replies to other people", skipped)
    return kept


def _require_creds(engine: Engine) -> dict[str, str]:
    creds = get_all_credentials(engine, NETWORK_TWITTER)
    missing = [k for k in ("bearer_token", "user_id", "username") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"X not configured (missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=twitter"
        )
    return creds


def get_bearer_token(engine: Engine) -> str:
    return _require_creds(engine)["bearer_token"]


def _format_api_error(status: int, detail: Any) -> str:
    if status == 402 and isinstance(detail, dict):
        title = detail.get("title", "")
        if title == "CreditsDepleted" or "credits" in str(detail.get("type", "")).lower():
            return (
                "X API credits depleted (HTTP 402).\n"
                "Your developer account has no remaining API credits for this request.\n"
                "Fix: https://developer.x.com/en/portal/dashboard → Billing / Products\n"
                "  • Free tier: small monthly allowance, resets each billing period\n"
                "  • Or purchase pay-as-you-go credits\n"
                "This is an X account limit — not a bug in this app."
            )
    return f"X API request failed ({status}): {detail}"


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
            detail: Any = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            raise RuntimeError(_format_api_error(response.status_code, detail))
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


async def authenticate(engine: Engine) -> None:
    """Prompt for bearer token and username, validate, store in SQLite."""
    print(AUTH_HELP)
    bearer_token = input("Bearer Token: ").strip()
    username = input("Username (without @): ").strip().lstrip("@")

    if not bearer_token or not username:
        raise RuntimeError("Bearer token and username are required.")

    user = await _lookup_user(bearer_token, username)
    set_credentials(
        engine,
        NETWORK_TWITTER,
        {
            "bearer_token": bearer_token,
            "user_id": str(user["id"]),
            "username": user["username"],
        },
    )
    print(f"X configured for @{user['username']}")


async def fetch_posts(
    engine: Engine,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    """Fetch posts from the user's own timeline (GET /2/users/:id/tweets).

    Uses API ``start_time`` when ``since`` is set so X only bills owned reads
    for tweets in range. ``max_pages`` caps pagination (watch mode).
    """
    creds = _require_creds(engine)
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
    # Always exclude retweets; keep replies (filter @-replies below)
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
        logger.info(
            "Timeline page %d: %d tweet(s) (%d total raw, %d kept so far)",
            page,
            len(tweets),
            raw_count,
            len(all_posts),
        )
        if since_utc and reached_since_boundary:
            logger.info("Reached --since boundary on page %d, stopping pagination", page)
            break
        if max_pages is not None and page >= max_pages:
            logger.info("Hit max_pages=%d, stopping pagination", max_pages)
            break
        if not pagination_token:
            break

    if skipped.get("retweet"):
        logger.info("Filtered out %d retweet(s)", skipped["retweet"])
    if skipped.get("quote"):
        logger.info("Filtered out %d quote tweet(s)", skipped["quote"])
    if skipped.get("at_reply"):
        logger.info("Filtered out %d @-reply tweet(s)", skipped["at_reply"])
    logger.info(
        "Timeline done: %d raw -> %d after filters (since=%s)",
        raw_count,
        len(all_posts),
        since.date() if since else "all",
    )

    all_posts.sort(key=lambda p: p.created_at)
    return filter_originals_and_threads(all_posts)


async def download_media(media: MediaItem, bearer_token: str) -> bytes:
    """Download media bytes from X CDN (public URLs often work without auth)."""
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        if response.status_code in (401, 403) and bearer_token:
            response = await client.get(
                media.url,
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
        response.raise_for_status()
        return response.content


async def publish_post(
    engine: Engine,
    post: Post,
    media_bytes: list[bytes] | None = None,
) -> str:
    raise NotImplementedError("X is configured as a source network only")
