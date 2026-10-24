import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post, PublishResult
from config import LINKEDIN_APP, NETWORK_LINKEDIN
from db.accounts import (
    Account,
    save_account,
    get_all_credentials,
    update_remote_id,
)
from utils.http_utils import format_api_error, parse_error_detail
from utils.posts import sort_chronologically

logger = logging.getLogger(__name__)

AUTH_HELP = """\
Configure LinkedIn account for mesh sync.

You will be asked for:
  1. Access Token — from https://www.linkedin.com/developers/apps/
       → your app → Auth → generate a member token with scopes:
         openid, profile, email, w_member_social, r_member_social
       → products required: Share on LinkedIn + Sign In with LinkedIn (OIDC)
  2. (optional) Display name — looked up from the token if omitted

Note: r_member_social (reading your posts) may require LinkedIn approval.
Without it, publishing still works but this account cannot act as a source.

Thread continuations are posted as comments on the previous part when the
platform has no native reply-to-post API.
"""

_LINKEDIN_IMAGE_PREFIX = "linkedin-image:"
_SHARE_URN_PREFIXES = ("urn:li:share:", "urn:li:ugcPost:")
_COMMENT_URN_PREFIX = "urn:li:comment:"
_HASHTAG_RE = re.compile(r"\{hashtag\|\\\#\|([^}]+)\}")
_MENTION_RE = re.compile(r"@\[([^\]]+)\]\([^)]+\)")


def _encode_urn(urn: str) -> str:
    return quote(urn, safe="")


def _api_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LINKEDIN_APP.api_version,
        "Content-Type": "application/json",
    }


def _api_error(response: httpx.Response) -> str:
    return format_api_error(
        "LinkedIn", response.status_code, parse_error_detail(response)
    )


def _require_creds(engine: Engine, account_id: int) -> dict[str, str]:
    creds = get_all_credentials(engine, account_id)
    missing = [k for k in ("access_token", "person_urn") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"LinkedIn account {account_id} not configured "
            f"(missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=linkedin"
        )
    return creds


def _is_post_urn(urn: str) -> bool:
    return urn.startswith(_SHARE_URN_PREFIXES)


def _is_comment_urn(urn: str) -> bool:
    return urn.startswith(_COMMENT_URN_PREFIX)


def _image_content_type(item: MediaItem) -> str:
    if item.media_type == "photo":
        return "image/jpeg"
    return "image/png"


def _photos_only(
    media: list[MediaItem], bytes_list: list[bytes], log_id: str
) -> list[tuple[MediaItem, bytes]]:
    photos: list[tuple[MediaItem, bytes]] = []
    for item, raw in zip(media, bytes_list):
        if item.media_type == "photo":
            photos.append((item, raw))
        else:
            logger.warning(
                "[%s] LinkedIn publish: skipping %s (video not supported yet)",
                log_id,
                item.media_type,
            )
    return photos


async def _api_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    access_token: str,
    *,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    response = await client.request(
        method,
        f"{LINKEDIN_APP.api_base_url}{path}",
        headers=_api_headers(access_token),
        json=json,
    )
    if not response.is_success:
        raise RuntimeError(_api_error(response))
    return response


async def _lookup_userinfo(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{LINKEDIN_APP.api_base_url}/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not response.is_success:
            raise RuntimeError(_api_error(response))
        return response.json()


async def authenticate(engine: Engine, label: str = "default", *, project_id: int = 1, sync_role: str | None = None, credentials: dict[str, str] | None = None) -> Account:
    report = print if credentials is None else logger.info
    access_token = (credentials.get("access_token", "") if credentials is not None else input("LinkedIn access token: ")).strip()
    if not access_token:
        raise ValueError("Access token is required")

    profile = await _lookup_userinfo(access_token)
    sub = profile.get("sub")
    if not sub:
        raise RuntimeError("LinkedIn userinfo: missing sub")

    person_urn = f"urn:li:person:{sub}"
    display_name = (
        (credentials.get("display_name", "") if credentials is not None else input("Display name (optional, Enter to auto-detect): ")).strip()
        or profile.get("name")
        or profile.get("email")
        or sub
    )

    creds = {
        "access_token": access_token,
        "person_urn": person_urn,
        "display_name": display_name,
    }

    account = save_account(engine, NETWORK_LINKEDIN, label, sub, creds, project_id=project_id, sync_role=sync_role)
    report(f"Configured {account.network}/{account.label}")
    return account


def _parse_ms_timestamp(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _strip_commentary(text: str) -> str:
    text = _MENTION_RE.sub(r"@\1", text)
    text = _HASHTAG_RE.sub(r"#\1", text)
    return text.replace("\\", "").strip()


def _image_media_item(image_urn: str, alt_text: str | None = None) -> MediaItem:
    return MediaItem(
        url=f"{_LINKEDIN_IMAGE_PREFIX}{image_urn}",
        media_type="photo",
        alt_text=alt_text,
    )


def _extract_content_media(content: dict[str, Any]) -> list[MediaItem]:
    if not content:
        return []

    items: list[MediaItem] = []
    media = content.get("media") or {}
    if media_id := media.get("id"):
        if str(media_id).startswith("urn:li:image:"):
            items.append(_image_media_item(str(media_id), media.get("altText")))

    multi = content.get("multiImage") or {}
    for image in multi.get("images") or []:
        image_id = image.get("id")
        if image_id and str(image_id).startswith("urn:li:image:"):
            items.append(_image_media_item(str(image_id), image.get("altText")))

    return items


def _element_to_post(element: dict[str, Any], author_id: str) -> Post | None:
    if element.get("reshareContext"):
        return None
    if element.get("lifecycleState") != "PUBLISHED":
        return None

    post_id = element.get("id")
    if not post_id:
        return None

    text = _strip_commentary(element.get("commentary") or "")
    media = _extract_content_media(element.get("content") or {})
    if not text and not media:
        return None

    created_ms = element.get("createdAt") or element.get("publishedAt")
    if created_ms is None:
        return None

    return Post(
        id=str(post_id),
        text=text,
        created_at=_parse_ms_timestamp(created_ms),
        conversation_id=str(post_id),
        author_id=author_id,
        media=media,
        is_thread_root=True,
    )


def _next_page_url(paging: dict[str, Any]) -> str | None:
    for link in paging.get("links") or []:
        if link.get("rel") == "next" and link.get("href"):
            return str(link["href"])
    return None


async def _fetch_author_page(
    client: httpx.AsyncClient,
    access_token: str,
    *,
    person_urn: str,
    start: int | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    if url:
        response = await client.get(url, headers=_api_headers(access_token))
    else:
        params: dict[str, str | int] = {
            "q": "author",
            "author": _encode_urn(person_urn),
            "sortBy": "CREATED",
            "count": 50,
            "viewContext": "AUTHOR",
        }
        if start is not None:
            params["start"] = start
        response = await client.get(
            f"{LINKEDIN_APP.api_base_url}/rest/posts",
            headers=_api_headers(access_token),
            params=params,
        )
    if not response.is_success:
        raise RuntimeError(_api_error(response))
    return response.json()


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    creds = _require_creds(engine, account_id)
    access_token = creds["access_token"]
    person_urn = creds["person_urn"]
    author_id = person_urn.rsplit(":", 1)[-1]
    since_utc = since.astimezone(timezone.utc) if since else None

    if not include_replies:
        logger.debug(
            "LinkedIn account %d: include_replies=False has no effect (no reply API)",
            account_id,
        )

    posts: list[Post] = []
    page = 0
    start = 0
    next_url: str | None = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            data = await _fetch_author_page(
                client,
                access_token,
                person_urn=person_urn,
                start=None if next_url else start,
                url=next_url,
            )
            elements = data.get("elements") or []
            if not elements:
                break

            reached_since = False
            for element in elements:
                post = _element_to_post(element, author_id)
                if not post:
                    continue
                if since_utc and post.created_at < since_utc:
                    reached_since = True
                    continue
                posts.append(post)

            if reached_since:
                break
            if max_pages is not None and page >= max_pages:
                break

            paging = data.get("paging") or {}
            next_url = _next_page_url(paging)
            if next_url:
                if next_url.startswith("/"):
                    next_url = f"{LINKEDIN_APP.api_base_url}{next_url}"
                continue
            start = int(paging.get("start", 0)) + int(paging.get("count", len(elements)))
            if start >= int(paging.get("total", start + 1)):
                break

    posts = sort_chronologically(posts)
    logger.info("LinkedIn fetch: %d post(s)", len(posts))
    return posts


async def _resolve_image_download_url(
    client: httpx.AsyncClient, access_token: str, image_urn: str
) -> str:
    response = await client.get(
        f"{LINKEDIN_APP.api_base_url}/rest/images/{_encode_urn(image_urn)}",
        headers=_api_headers(access_token),
    )
    if not response.is_success:
        raise RuntimeError(_api_error(response))
    data = response.json()
    download_url = data.get("downloadUrl")
    if not download_url and isinstance(data.get("value"), dict):
        download_url = data["value"].get("downloadUrl")
    if not download_url:
        raise RuntimeError(f"LinkedIn image {image_urn}: missing downloadUrl")
    return str(download_url)


async def download_media(
    media: MediaItem, engine: Engine, account_id: int
) -> bytes:
    if media.url.startswith(_LINKEDIN_IMAGE_PREFIX):
        image_urn = media.url[len(_LINKEDIN_IMAGE_PREFIX) :]
        creds = _require_creds(engine, account_id)
        async with httpx.AsyncClient(timeout=120.0) as client:
            download_url = await _resolve_image_download_url(
                client, creds["access_token"], image_urn
            )
            response = await client.get(download_url)
            response.raise_for_status()
            return response.content

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(media.url)
        response.raise_for_status()
        return response.content


async def _upload_image(
    client: httpx.AsyncClient,
    access_token: str,
    person_urn: str,
    raw: bytes,
    item: MediaItem,
) -> str:
    init_response = await _api_request(
        client,
        "POST",
        "/rest/images?action=initializeUpload",
        access_token,
        json={"initializeUploadRequest": {"owner": person_urn}},
    )
    value = init_response.json().get("value") or {}
    upload_url = value.get("uploadUrl")
    image_urn = value.get("image")
    if not upload_url or not image_urn:
        raise RuntimeError(
            f"LinkedIn initializeUpload: missing uploadUrl/image in {value}"
        )

    upload_response = await client.put(
        upload_url,
        content=raw,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": _image_content_type(item),
        },
    )
    if upload_response.status_code not in (200, 201):
        raise RuntimeError(
            f"LinkedIn image upload failed ({upload_response.status_code}): "
            f"{upload_response.text[:200]}"
        )
    return str(image_urn)


def _post_payload(
    person_urn: str,
    commentary: str,
    images: list[tuple[str, str | None]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "author": person_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    if not images:
        return payload

    if len(images) == 1:
        image_urn, alt_text = images[0]
        media: dict[str, Any] = {"id": image_urn}
        if alt_text:
            media["altText"] = alt_text
        payload["content"] = {"media": media}
    else:
        payload["content"] = {
            "multiImage": {
                "images": [
                    {"id": urn, **({"altText": alt} if alt else {})}
                    for urn, alt in images
                ]
            }
        }

    return payload


def _extract_post_id(response: httpx.Response) -> str:
    post_id = response.headers.get("x-restli-id")
    if post_id:
        return post_id
    location = response.headers.get("location") or ""
    if location:
        return location.rstrip("/").rsplit("/", 1)[-1]
    raise RuntimeError("LinkedIn create post: missing x-restli-id in response")


async def _create_post(
    client: httpx.AsyncClient,
    access_token: str,
    person_urn: str,
    commentary: str,
    images: list[tuple[str, str | None]],
) -> str:
    response = await _api_request(
        client,
        "POST",
        "/rest/posts",
        access_token,
        json=_post_payload(person_urn, commentary, images),
    )
    return _extract_post_id(response)


async def _create_comment(
    client: httpx.AsyncClient,
    access_token: str,
    person_urn: str,
    text: str,
    reply_to: str,
) -> str:
    if _is_comment_urn(reply_to):
        parent_post_urn = reply_to.split("(", 1)[1].rsplit(",", 1)[0]
        body: dict[str, Any] = {
            "actor": person_urn,
            "message": {"text": text},
            "object": parent_post_urn,
            "parentComment": reply_to,
        }
        path_urn = parent_post_urn
    elif _is_post_urn(reply_to):
        body = {
            "actor": person_urn,
            "message": {"text": text},
            "object": reply_to,
        }
        path_urn = reply_to
    else:
        raise RuntimeError(f"LinkedIn comment: unsupported reply_to URN: {reply_to}")

    response = await _api_request(
        client,
        "POST",
        f"/rest/socialActions/{_encode_urn(path_urn)}/comments",
        access_token,
        json=body,
    )
    comment_id = response.headers.get("x-restli-id")
    if comment_id:
        return comment_id
    raise RuntimeError("LinkedIn create comment: missing x-restli-id in response")


async def publish_outbound(
    engine: Engine,
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    creds = _require_creds(engine, account_id)
    access_token = creds["access_token"]
    person_urn = creds["person_urn"]
    text = outbound.text or ""
    media = outbound.media
    bytes_list = media_bytes or []
    log_id = outbound.source_post_ids[0] if outbound.source_post_ids else "?"

    if media and len(bytes_list) != len(media):
        raise RuntimeError(
            f"Media upload mismatch for post {log_id}: "
            f"{len(media)} attachment(s) but {len(bytes_list)} downloaded"
        )

    photos = _photos_only(media, bytes_list, log_id) if media else []

    async with httpx.AsyncClient(timeout=120.0) as client:
        if reply_to:
            if photos:
                logger.warning(
                    "[%s] LinkedIn thread continuation: posting text only "
                    "(comments API does not support inline images)",
                    log_id,
                )
            comment_id = await _create_comment(
                client, access_token, person_urn, text, reply_to
            )
            return PublishResult(post_id=comment_id, reply_ref=comment_id)

        uploaded: list[tuple[str, str | None]] = []
        for item, raw in photos:
            image_urn = await _upload_image(
                client, access_token, person_urn, raw, item
            )
            uploaded.append((image_urn, item.alt_text))

        post_id = await _create_post(
            client, access_token, person_urn, text, uploaded
        )
        return PublishResult(post_id=post_id, reply_ref=post_id)
