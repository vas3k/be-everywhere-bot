import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post, sort_chronologically
from config import BLUESKY_APP, NETWORK_BLUESKY
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credentials,
    update_remote_id,
)

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure Bluesky account for mesh sync.

You will be asked for:
  1. Handle — your Bluesky handle (e.g. user.bsky.social)
  2. App Password — from Bluesky Settings → Privacy and security → App passwords
       (not your account login password)

Optional: custom PDS URL (press Enter for https://bsky.social).

Credentials are stored per account label in the local SQLite database.
"""

IMAGE_MAX_BYTES = 1_000_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _rkey_from_uri(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _did_from_uri(uri: str) -> str:
    return uri.split("/")[2]


def _xrpc_url(pds_url: str, method: str) -> str:
    return f"{pds_url.rstrip('/')}/xrpc/{method}"


def _require_creds(engine: Engine, account_id: int) -> dict[str, str]:
    creds = get_all_credentials(engine, account_id)
    missing = [
        k
        for k in ("handle", "did", "access_jwt", "pds_url")
        if not creds.get(k)
    ]
    if missing:
        raise RuntimeError(
            f"Bluesky account {account_id} not configured (missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=bluesky"
        )
    return creds


def _format_api_error(status: int, detail: Any) -> str:
    if isinstance(detail, dict):
        msg = detail.get("message") or detail.get("error") or detail
        return f"Bluesky API request failed ({status}): {msg}"
    return f"Bluesky API request failed ({status}): {detail}"


async def _api_get(
    pds_url: str,
    access_jwt: str,
    method: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_jwt}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            _xrpc_url(pds_url, method),
            headers=headers,
            params=params,
        )
        if not response.is_success:
            detail: Any = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            raise RuntimeError(_format_api_error(response.status_code, detail))
        return response.json()


async def _api_post_json(
    pds_url: str,
    access_jwt: str,
    method: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_jwt}"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            _xrpc_url(pds_url, method),
            headers=headers,
            json=body,
        )
        if not response.is_success:
            detail: Any = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            raise RuntimeError(_format_api_error(response.status_code, detail))
        return response.json()


async def _api_post_bytes(
    pds_url: str,
    access_jwt: str,
    method: str,
    content: bytes,
    content_type: str,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_jwt}",
        "Content-Type": content_type,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            _xrpc_url(pds_url, method),
            headers=headers,
            content=content,
        )
        if not response.is_success:
            detail: Any = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            raise RuntimeError(_format_api_error(response.status_code, detail))
        return response.json()


def _extract_media(embed: dict[str, Any] | None) -> list[MediaItem]:
    if not embed:
        return []

    embed_type = embed.get("$type", "")
    items: list[MediaItem] = []

    if embed_type == "app.bsky.embed.images#view":
        for image in embed.get("images") or []:
            url = image.get("fullsize") or image.get("thumb")
            if not url:
                continue
            items.append(
                MediaItem(
                    url=url,
                    media_type="photo",
                    alt_text=image.get("alt"),
                )
            )
        return items

    if embed_type == "app.bsky.embed.video#view":
        url = embed.get("playlist") or embed.get("thumbnail")
        if url:
            items.append(MediaItem(url=url, media_type="video"))
        return items

    if embed_type == "app.bsky.embed.external#view":
        external = embed.get("external") or {}
        thumb = external.get("thumb")
        if isinstance(thumb, str):
            items.append(MediaItem(url=thumb, media_type="photo"))
        return items

    return items


def _skip_reason(item: dict[str, Any], own_did: str) -> str | None:
    if item.get("reason"):
        return "repost"

    post = item.get("post") or {}
    record = post.get("record") or {}
    embed = record.get("embed") or {}
    if embed.get("$type") == "app.bsky.embed.record":
        return "quote"
    if record.get("text", "").lstrip().startswith("@"):
        return "at_reply"

    reply = record.get("reply")
    if reply:
        root_uri = reply.get("root", {}).get("uri", "")
        if root_uri and _did_from_uri(root_uri) != own_did:
            return "foreign_reply"
    return None


def _feed_item_to_post(item: dict[str, Any], own_did: str) -> Post:
    post = item["post"]
    record = post["record"]
    uri = post["uri"]
    post_id = _rkey_from_uri(uri)

    reply = record.get("reply")
    if reply:
        root_uri = reply["root"]["uri"]
        conversation_id = _rkey_from_uri(root_uri)
        parent_uri = reply["parent"]["uri"]
        in_reply_to_id = _rkey_from_uri(parent_uri)
    else:
        conversation_id = post_id
        in_reply_to_id = None

    media = _extract_media(post.get("embed"))
    text = record.get("text", "")

    return Post(
        id=post_id,
        text=text,
        created_at=_parse_datetime(record["createdAt"]),
        conversation_id=conversation_id,
        author_id=own_did,
        media=media,
        in_reply_to_id=in_reply_to_id,
        in_reply_to_user_id=own_did if in_reply_to_id else None,
        is_thread_root=conversation_id == post_id,
    )


def filter_originals_and_threads(posts: list[Post]) -> list[Post]:
    if not posts:
        return []

    own_ids = {p.id for p in posts}
    own_thread_roots = {p.id for p in posts if p.conversation_id == p.id}

    kept: list[Post] = []
    skipped = 0

    for post in posts:
        if post.in_reply_to_id is not None:
            in_own_thread = (
                post.in_reply_to_id in own_ids
                or post.conversation_id in own_thread_roots
            )
            if not in_own_thread:
                skipped += 1
                continue
        kept.append(post)

    if skipped:
        logger.info("Filtered out %d Bluesky reply/replies to other people", skipped)
    return kept


def _media_content_type(item: MediaItem) -> str:
    if item.media_type == "photo":
        return "image/jpeg"
    return "video/mp4"


async def _upload_blob(
    pds_url: str,
    access_jwt: str,
    raw: bytes,
    item: MediaItem,
) -> dict[str, Any]:
    if item.media_type == "photo" and len(raw) > IMAGE_MAX_BYTES:
        raise RuntimeError(
            f"Bluesky image too large ({len(raw)} bytes, max {IMAGE_MAX_BYTES})"
        )
    result = await _api_post_bytes(
        pds_url,
        access_jwt,
        "com.atproto.repo.uploadBlob",
        raw,
        _media_content_type(item),
    )
    blob = result.get("blob")
    if not blob:
        raise RuntimeError("Bluesky uploadBlob: missing blob in response")
    return blob


async def _get_reply_refs(
    pds_url: str,
    access_jwt: str,
    parent_uri: str,
    parent_cid: str,
) -> dict[str, dict[str, str]]:
    uri_parts = parent_uri.replace("at://", "").split("/")
    repo, collection, rkey = uri_parts[0], uri_parts[1], uri_parts[2]
    parent = await _api_get(
        pds_url,
        access_jwt,
        "com.atproto.repo.getRecord",
        {"repo": repo, "collection": collection, "rkey": rkey},
    )
    parent_reply = parent.get("value", {}).get("reply")
    if parent_reply:
        root = {"uri": parent_reply["root"]["uri"], "cid": parent_reply["root"]["cid"]}
    else:
        root = {"uri": parent["uri"], "cid": parent["cid"]}
    return {
        "root": root,
        "parent": {"uri": parent_uri, "cid": parent_cid},
    }


async def _build_embed(
    pds_url: str,
    access_jwt: str,
    media: list[MediaItem],
    media_bytes: list[bytes],
) -> dict[str, Any] | None:
    if not media:
        return None

    images: list[dict[str, Any]] = []
    video_blob: dict[str, Any] | None = None

    for item, raw in zip(media, media_bytes):
        if item.media_type == "photo" and len(images) < 4:
            blob = await _upload_blob(pds_url, access_jwt, raw, item)
            images.append(
                {
                    "alt": item.alt_text or "",
                    "image": blob,
                }
            )
        elif item.media_type in ("video", "animated_gif") and video_blob is None:
            video_blob = await _upload_blob(pds_url, access_jwt, raw, item)

    if images:
        return {"$type": "app.bsky.embed.images", "images": images}
    if video_blob:
        return {"$type": "app.bsky.embed.video", "video": video_blob}
    return None


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    handle = input("Handle (e.g. user.bsky.social): ").strip().lstrip("@")
    app_password = input("App Password: ").strip()
    pds_url = input("PDS URL (optional): ").strip() or BLUESKY_APP.default_pds

    if not handle or not app_password:
        raise RuntimeError("Handle and app password are required.")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            _xrpc_url(pds_url, "com.atproto.server.createSession"),
            json={"identifier": handle, "password": app_password},
        )
        if not response.is_success:
            detail: Any = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            raise RuntimeError(_format_api_error(response.status_code, detail))
        session = response.json()

    creds = {
        "handle": session.get("handle", handle),
        "did": session["did"],
        "access_jwt": session["accessJwt"],
        "refresh_jwt": session.get("refreshJwt", ""),
        "pds_url": pds_url,
    }

    existing = find_account(engine, NETWORK_BLUESKY, label)
    if existing:
        set_credentials(engine, existing.id, creds)
        update_remote_id(engine, existing.id, creds["did"])
        print(f"Bluesky account '{label}' updated for @{creds['handle']}")
        return existing

    account = create_account(engine, NETWORK_BLUESKY, label, creds["did"])
    set_credentials(engine, account.id, creds)
    print(f"Bluesky account '{label}' configured for @{creds['handle']}")
    return account


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    creds = _require_creds(engine, account_id)
    pds_url = creds["pds_url"]
    access_jwt = creds["access_jwt"]
    actor = creds["did"]
    own_did = creds["did"]
    since_utc = since.astimezone(timezone.utc) if since else None

    posts: list[Post] = []
    seen_ids: set[str] = set()
    skipped: dict[str, int] = {}
    cursor: str | None = None
    page = 0

    while True:
        page += 1
        params: dict[str, str] = {
            "actor": actor,
            "limit": "50",
            "includePins": "true",
        }
        if not include_replies:
            params["filter"] = "posts_no_replies"
        if cursor:
            params["cursor"] = cursor

        data = await _api_get(
            pds_url, access_jwt, "app.bsky.feed.getAuthorFeed", params
        )
        feed = data.get("feed") or []
        if not feed:
            break

        reached_since = False
        for item in feed:
            post_view = item.get("post") or {}
            uri = post_view.get("uri")
            if not uri:
                continue
            post_id = _rkey_from_uri(uri)
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            record = post_view.get("record") or {}
            created_at = _parse_datetime(record.get("createdAt", _now_iso()))
            if since_utc and created_at < since_utc:
                reached_since = True
                continue

            reason = _skip_reason(item, own_did)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue

            posts.append(_feed_item_to_post(item, own_did))

        cursor = data.get("cursor")
        if reached_since or not cursor:
            break
        if max_pages is not None and page >= max_pages:
            break

    if skipped:
        logger.info(
            "Bluesky fetch skipped: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())),
        )

    posts = sort_chronologically(posts)
    return filter_originals_and_threads(posts)


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
    reply_to: tuple[str, str] | None = None,
) -> tuple[str, tuple[str, str]]:
    """Publish to Bluesky. Returns (post_id, (uri, cid)) for reply chaining."""
    creds = _require_creds(engine, account_id)
    pds_url = creds["pds_url"]
    access_jwt = creds["access_jwt"]
    did = creds["did"]
    text = outbound.text or ""
    media = outbound.media
    bytes_list = media_bytes or []

    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": _now_iso(),
    }

    if media and bytes_list:
        embed = await _build_embed(pds_url, access_jwt, media, bytes_list)
        if embed:
            record["embed"] = embed
        elif media:
            logger.warning("Bluesky publish: media upload failed — posting text only")

    if reply_to:
        parent_uri, parent_cid = reply_to
        record["reply"] = await _get_reply_refs(
            pds_url, access_jwt, parent_uri, parent_cid
        )

    result = await _api_post_json(
        pds_url,
        access_jwt,
        "com.atproto.repo.createRecord",
        {
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": record,
        },
    )
    uri = result.get("uri")
    cid = result.get("cid")
    if not uri or not cid:
        raise RuntimeError(f"Bluesky createRecord: missing uri/cid in {result}")
    return _rkey_from_uri(uri), (uri, cid)


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
    post_id, _ = await publish_outbound(engine, account_id, outbound, media_bytes)
    return post_id
