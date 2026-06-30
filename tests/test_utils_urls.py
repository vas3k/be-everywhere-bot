import httpx
import pytest
from datetime import datetime, timezone

from apis.types import Post
from utils.urls import (
    _find_urls,
    is_shortener,
    public_https_url,
    should_unwrap,
    unwrap_posts_text,
    unwrap_url,
    unwrap_urls_in_text,
)


class _FakeResponse:
    def __init__(self, url: str):
        self.url = url


def test_is_shortener():
    assert is_shortener("https://t.co/abc") is True
    assert is_shortener("https://bit.ly/xyz") is True
    assert is_shortener("https://example.com/page") is False


def test_public_https_url():
    assert public_https_url("https://cdn.example/a.jpg") is True
    assert public_https_url("tgfile:abc") is False
    assert public_https_url("") is False


def test_should_unwrap_matches_shortener():
    assert should_unwrap("https://t.co/abc") is True
    assert should_unwrap("https://mastodon.social/@user") is False


def test_find_urls_deduplicates():
    text = "See https://t.co/a and https://t.co/a again"
    urls = _find_urls(text)
    assert urls == ["https://t.co/a"]


def test_find_urls_strips_trailing_punctuation():
    text = "Link: https://t.co/abc)."
    assert _find_urls(text) == ["https://t.co/abc"]


@pytest.mark.asyncio
async def test_unwrap_url_follows_redirect():
    async with httpx.AsyncClient() as client:

        async def fake_get(url: str, **_kwargs):
            assert url == "https://t.co/abc"
            return _FakeResponse("https://example.com/full")

        client.get = fake_get  # type: ignore[method-assign]
        final = await unwrap_url("https://t.co/abc", client=client, cache={})
    assert final.rstrip("/").endswith("example.com/full")


@pytest.mark.asyncio
async def test_unwrap_url_uses_cache():
    cache = {"https://t.co/abc": "https://example.com/cached"}
    async with httpx.AsyncClient() as client:
        final = await unwrap_url("https://t.co/abc", client=client, cache=cache)
    assert final == "https://example.com/cached"


@pytest.mark.asyncio
async def test_unwrap_url_skips_non_shortener():
    async with httpx.AsyncClient() as client:
        final = await unwrap_url("https://example.com/page", client=client, cache={})
    assert final == "https://example.com/page"


@pytest.mark.asyncio
async def test_unwrap_urls_in_text_replaces_shorteners():
    async with httpx.AsyncClient() as client:

        async def fake_get(url: str, **_kwargs):
            return _FakeResponse("https://example.com/article")

        client.get = fake_get  # type: ignore[method-assign]
        result = await unwrap_urls_in_text(
            "Read https://t.co/abc now",
            client=client,
            cache={},
        )
    assert "https://t.co/abc" not in result
    assert "example.com/article" in result


@pytest.mark.asyncio
async def test_unwrap_posts_text_mutates_posts_in_place(monkeypatch):
    post = Post(
        id="1",
        text="Read https://t.co/abc now",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="1",
        author_id="author",
    )

    async def fake_unwrap(text: str, *, client, cache):
        return text.replace("https://t.co/abc", "https://example.com/article")

    monkeypatch.setattr("utils.urls.unwrap_urls_in_text", fake_unwrap)
    await unwrap_posts_text([post])
    assert "https://t.co/abc" not in post.text
    assert "example.com/article" in post.text


@pytest.mark.asyncio
async def test_unwrap_posts_text_noop_for_empty_list():
    await unwrap_posts_text([])
