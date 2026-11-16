import json
from dataclasses import replace

import httpx
import pytest
import respx

from apis.rss import _parse_feed
from config import NETWORK_LIMITS
from db.posts import load_posts, save_posts
from utils.rss_summaries import summarize_rss_posts


@pytest.fixture
def rss_post(post_factory):
    return replace(post_factory(), rss_title="Тысяча шагов каждый день", rss_link="https://vas3k.club/post/123/",
                   rss_context="Прогулки помогают выработать привычку. " * 30)


@pytest.mark.parametrize("network", ["twitter", "bluesky", "mastodon", "threads"])
@respx.mock
async def test_summary_keeps_link_before_summary_and_media(network, rss_post, photo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    rss_post.media = [photo]
    route = respx.post("https://api.openai.com/v1/responses").mock(return_value=httpx.Response(200, json={
        "status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps({"summary": "Короткие прогулки помогают закрепить привычку."})}]}],
    }))
    await summarize_rss_posts([rss_post], network)
    assert rss_post.text == f"{rss_post.rss_title}\n\n{rss_post.rss_link}\n\nКороткие прогулки помогают закрепить привычку."
    assert len(rss_post.text) <= NETWORK_LIMITS[network].max_text
    assert rss_post.media == [photo]
    request = json.loads(route.calls[0].request.content)
    assert request["store"] is False
    assert len(json.loads(request["input"])["excerpt"]) <= 5000


@pytest.mark.parametrize("case", ["dry_run", "short_context", "no_key", "http_error", "malformed", "incomplete", "too_long", "first_person"])
@respx.mock
async def test_unavailable_summary_falls_back_without_blocking(case, rss_post, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key" if case != "no_key" else "")
    if case == "short_context":
        rss_post.rss_context = "Закрытый пост"
    payload = {"status": "completed", "output": [{"type": "message", "content": [{
        "type": "output_text", "text": json.dumps({"summary": "я ходил гулять" if case == "first_person" else "Слово " * 100})}]}]}
    if case == "malformed":
        payload = {"status": "completed"}
    if case == "incomplete":
        payload["status"] = "incomplete"
    route = respx.post("https://api.openai.com/v1/responses").mock(return_value=httpx.Response(
        429 if case == "http_error" else 200, json=payload))
    await summarize_rss_posts([rss_post], "bluesky", dry_run=case == "dry_run")
    assert rss_post.text == f"{rss_post.rss_title}\n\n{rss_post.rss_link}"
    if case in ("dry_run", "short_context", "no_key"):
        assert not route.called
    assert route.call_count <= 2


async def test_long_title_preserves_whole_link_without_extra_parts(rss_post):
    rss_post.rss_title = "Длинный заголовок 🐈 " * 40
    await summarize_rss_posts([rss_post], "twitter", dry_run=True)
    assert rss_post.text.endswith(rss_post.rss_link)
    assert sum(2 if ord(c) > 4351 else 1 for c in rss_post.text) <= 280


def test_rss_context_survives_cache_and_old_posts_still_load(engine, post_factory):
    parsed = _parse_feed('<rss><channel><title>Club</title><item><title>Title</title><link>https://example.com/p</link>'
                         '<description>' + 'Public text ' * 600 + '</description></item></channel></rss>', 'https://example.com/rss')[0]
    assert len(parsed.rss_context) == 5000
    from db.accounts import create_account
    account = create_account(engine, "rss", "feed", "feed")
    with engine.begin() as connection:
        save_posts(connection, account.id, [parsed, post_factory("old")])
    cached = {post.id: post for post in load_posts(engine, account.id)}
    assert cached[parsed.id].rss_context == parsed.rss_context
    assert cached["old"].rss_context == ""
