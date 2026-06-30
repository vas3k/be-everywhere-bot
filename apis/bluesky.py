from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from utils.http_utils import format_api_error, parse_error_detail
from apis.types import MediaItem, OutboundPost, Post, PublishResult
from utils.posts import sort_chronologically
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


def _is_token_expired(status: int, detail: Any) -> bool:
    if status not in (400, 401):
        return False
    if isinstance(detail, dict):
        msg = str(detail.get("message") or detail.get("error") or "").lower()
    else:
        msg = str(detail).lower()
    return "expired" in msg or "invalid token" in msg


class BlueskySession:
    """Authenticated Bluesky PDS client with automatic token refresh."""

    def __init__(self, engine: Engine, account_id: int, creds: dict[str, str]):
        self.engine = engine
        self.account_id = account_id
        self.creds = creds
        self.pds_url = creds["pds_url"]

    @classmethod
    def load(cls, engine: Engine, account_id: int) -> BlueskySession:
        return cls(engine, account_id, _require_creds(engine, account_id))

    @property
    def did(self) -> str:
        return self.creds["did"]

    @property
    def access_jwt(self) -> str:
        return self.creds["access_jwt"]

    async def get(
        self, method: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await self._request("GET", method, params=params)

    async def post_json(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", method, json_body=body)

    async def post_bytes(
        self, method: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST", method, content=content, content_type=content_type
        )

    async def _request(
        self,
        http_method: str,
        xrpc_method: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.access_jwt}"}
        if content_type:
            headers["Content-Type"] = content_type
        timeout = 120.0 if http_method == "POST" else 60.0
        url = _xrpc_url(self.pds_url, xrpc_method)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if http_method == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif content is not None:
                response = await client.post(url, headers=headers, content=content)
            else:
                response = await client.post(url, headers=headers, json=json_body)

        if response.is_success:
            return response.json()

        detail = parse_error_detail(response)
        if (
            not _retried
            and _is_token_expired(response.status_code, detail)
        ):
            await _refresh_session(self.engine, self.account_id, self.creds)
            return await self._request(
                http_method,
                xrpc_method,
                params=params,
                json_body=json_body,
                content=content,
                content_type=content_type,
                _retried=True,
            )
        raise RuntimeError(format_api_error("Bluesky", response.status_code, detail))


async def _refresh_session(
    engine: Engine, account_id: int, creds: dict[str, str]
) -> str:
    refresh_jwt = creds.get("refresh_jwt")
    if not refresh_jwt:
        raise RuntimeError(
            f"Bluesky account {account_id} refresh token missing. "
            "Run: uv run python main.py --auth=bluesky"
        )

    pds_url = creds["pds_url"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            _xrpc_url(pds_url, "com.atproto.server.refreshSession"),
            headers={"Authorization": f"Bearer {refresh_jwt}"},
        )
        if not response.is_success:
            detail = parse_error_detail(response)
            raise RuntimeError(format_api_error("Bluesky", response.status_code, detail))
        session = response.json()

    new_access = session["accessJwt"]
    updates = {"access_jwt": new_access}
    if session.get("refreshJwt"):
        updates["refresh_jwt"] = session["refreshJwt"]
    set_credentials(engine, account_id, updates)
    creds.update(updates)
    logger.info("Refreshed Bluesky session for account %d", account_id)
    return new_access


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


def _skip_reason(item: dict[str, Any]) -> str | None:
    if item.get("reason"):
        return "repost"

    post = item.get("post") or {}
    record = post.get("record") or {}
    embed = record.get("embed") or {}
    if embed.get("$type") == "app.bsky.embed.record":
        return "quote"
    if record.get("text", "").lstrip().startswith("@"):
        return "at_reply"
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


REPLY_REF_SEP = "\x1f"


def make_reply_ref(uri: str, cid: str) -> str:
    return f"{uri}{REPLY_REF_SEP}{cid}"


def _parse_reply_ref(reply_ref: str) -> tuple[str, str]:
    uri, cid = reply_ref.split(REPLY_REF_SEP, 1)
    return uri, cid



def _media_content_type(item: MediaItem) -> str:
    if item.media_type == "photo":
        return "image/jpeg"
    return "video/mp4"


async def _upload_blob(
    session: BlueskySession,
    raw: bytes,
    item: MediaItem,
) -> dict[str, Any]:
    if item.media_type == "photo" and len(raw) > IMAGE_MAX_BYTES:
        raise RuntimeError(
            f"Bluesky image too large ({len(raw)} bytes, max {IMAGE_MAX_BYTES})"
        )
    result = await session.post_bytes(
        "com.atproto.repo.uploadBlob",
        raw,
        _media_content_type(item),
    )
    blob = result.get("blob")
    if not blob:
        raise RuntimeError("Bluesky uploadBlob: missing blob in response")
    return blob


async def _get_reply_refs(
    session: BlueskySession,
    parent_uri: str,
    parent_cid: str,
) -> dict[str, dict[str, str]]:
    uri_parts = parent_uri.replace("at://", "").split("/")
    repo, collection, rkey = uri_parts[0], uri_parts[1], uri_parts[2]
    parent = await session.get(
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
    session: BlueskySession,
    media: list[MediaItem],
    media_bytes: list[bytes],
) -> dict[str, Any] | None:
    if not media:
        return None

    images: list[dict[str, Any]] = []
    video_blob: dict[str, Any] | None = None

    for item, raw in zip(media, media_bytes):
        if item.media_type == "photo" and len(images) < 4:
            blob = await _upload_blob(session, raw, item)
            images.append(
                {
                    "alt": item.alt_text or "",
                    "image": blob,
                }
            )
        elif item.media_type in ("video", "animated_gif") and video_blob is None:
            video_blob = await _upload_blob(session, raw, item)

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
            detail = parse_error_detail(response)
            raise RuntimeError(format_api_error("Bluesky", response.status_code, detail))
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
    session = BlueskySession.load(engine, account_id)
    own_did = session.did
    since_utc = since.astimezone(timezone.utc) if since else None

    posts: list[Post] = []
    seen_ids: set[str] = set()
    skipped: dict[str, int] = {}
    cursor: str | None = None
    page = 0

    while True:
        page += 1
        params: dict[str, str] = {
            "actor": own_did,
            "limit": "50",
            "includePins": "true",
        }
        if not include_replies:
            params["filter"] = "posts_no_replies"
        if cursor:
            params["cursor"] = cursor

        data = await session.get("app.bsky.feed.getAuthorFeed", params)
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

            reason = _skip_reason(item)
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

    return sort_chronologically(posts)


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        response.raise_for_status()
        return response.content


async def resolve_reply_target(
    engine: Engine, account_id: int, post_id: str
) -> str:
    """Resolve a stored Bluesky post id (rkey) to a reply_ref for chaining."""
    session = BlueskySession.load(engine, account_id)
    uri = f"at://{session.did}/app.bsky.feed.post/{post_id}"
    record = await session.get(
        "com.atproto.repo.getRecord",
        {"repo": session.did, "collection": "app.bsky.feed.post", "rkey": post_id},
    )
    cid = record.get("cid")
    if not cid:
        raise RuntimeError(f"Bluesky getRecord: missing cid for {uri}")
    return make_reply_ref(uri, cid)


async def publish_outbound(
    engine: Engine,
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    """Publish to Bluesky."""
    session = BlueskySession.load(engine, account_id)
    text = outbound.text or ""
    media = outbound.media
    bytes_list = media_bytes or []

    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": _now_iso(),
    }

    if media and bytes_list:
        embed = await _build_embed(session, media, bytes_list)
        if embed:
            record["embed"] = embed
        elif media:
            logger.warning("Bluesky publish: media upload failed — posting text only")

    if reply_to:
        parent_uri, parent_cid = _parse_reply_ref(reply_to)
        record["reply"] = await _get_reply_refs(session, parent_uri, parent_cid)

    result = await session.post_json(
        "com.atproto.repo.createRecord",
        {
            "repo": session.did,
            "collection": "app.bsky.feed.post",
            "record": record,
        },
    )
    uri = result.get("uri")
    cid = result.get("cid")
    if not uri or not cid:
        raise RuntimeError(f"Bluesky createRecord: missing uri/cid in {result}")
    post_id = _rkey_from_uri(uri)
    return PublishResult(post_id=post_id, reply_ref=make_reply_ref(uri, cid))
