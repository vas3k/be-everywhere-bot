"""Universal redirect unwrapping for shortened links in post text."""

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Hosts that wrap real destinations (extend as new sources are added)
SHORTENER_HOSTS = frozenset(
    {
        "t.co",
        "bit.ly",
        "buff.ly",
        "goo.gl",
        "ow.ly",
        "tinyurl.com",
        "fb.me",
        "lnkd.in",
        "youtu.be",
    }
)

URL_IN_TEXT = re.compile(r"""https?://[^\s<>"'\)\]\}]+""", re.IGNORECASE)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def is_shortener(url: str) -> bool:
    """True if URL is a known shortener/redirect wrapper."""
    host = _host(url)
    return host in SHORTENER_HOSTS or host.endswith(".t.co")


def should_unwrap(url: str) -> bool:
    return is_shortener(url)


async def unwrap_url(
    url: str,
    *,
    client: httpx.AsyncClient,
    cache: dict[str, str],
) -> str:
    """Follow redirects and return the final URL."""
    if url in cache:
        return cache[url]
    if not should_unwrap(url):
        cache[url] = url
        return url

    try:
        response = await client.get(url)
        final = str(response.url).rstrip("/")
        # Drop twitter/card query junk but keep the link
        cache[url] = final
        logger.debug("Unwrapped %s -> %s", url, final)
        return final
    except Exception as exc:
        logger.warning("Could not unwrap %s: %s", url, exc)
        cache[url] = url
        return url


def _find_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_IN_TEXT.finditer(text):
        raw = match.group(0).rstrip(".,;:!?)")
        if raw not in seen:
            seen.add(raw)
            urls.append(raw)
    return urls


async def unwrap_urls_in_text(
    text: str,
    *,
    client: httpx.AsyncClient,
    cache: dict[str, str],
) -> str:
    """Replace every shortener URL in text with its resolved destination."""
    if not text:
        return text
    for short in _find_urls(text):
        if not should_unwrap(short):
            continue
        long = await unwrap_url(short, client=client, cache=cache)
        if long != short:
            text = text.replace(short, long)
    return text


async def unwrap_posts_text(posts: list) -> None:
    """Resolve short links in place for a list of Post objects."""
    if not posts:
        return
    cache: dict[str, str] = {}
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "be-everywhere-bot/1.0"},
    ) as client:
        for post in posts:
            if post.text:
                post.text = await unwrap_urls_in_text(
                    post.text, client=client, cache=cache
                )
