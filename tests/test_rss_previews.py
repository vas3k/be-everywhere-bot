from datetime import datetime, timezone

import httpx
import pytest
import respx

from apis.bluesky import _build_embed
from apis.rss import fetch_posts
from apis.types import MediaItem
from db.accounts import create_account, set_credentials
from db.posts import load_posts, save_posts


@pytest.mark.asyncio
@respx.mock
async def test_rss_preview_cutoff_and_cache(engine):
    account = create_account(engine, 'rss', 'preview', 'feed', sync_role='source_only')
    set_credentials(engine, account.id, {
        'feed_url': 'https://example.com/feed', 'title_only': 'true',
        'link_previews': 'true', 'start_after': '2026-11-15T10:00:00+00:00',
    })
    respx.get('https://example.com/feed').mock(return_value=httpx.Response(200, text='''
      <rss><channel><title>Feed</title>
        <item><title>Old</title><link>https://example.com/old</link>
          <pubDate>Sun, 15 Nov 2026 09:00:00 +0000</pubDate></item>
        <item><title>New</title><description>Summary</description>
          <link>https://example.com/new</link>
          <pubDate>Sun, 15 Nov 2026 11:00:00 +0000</pubDate></item>
      </channel></rss>'''))
    respx.get('https://example.com/new').mock(return_value=httpx.Response(200, text='''
      <meta content="/preview.png" property="og:image">
      <meta property="og:title" content="New &amp; interesting">
      <meta property="og:description" content="Card description">'''))
    posts = await fetch_posts(engine, account.id, since=datetime(2026, 11, 14, tzinfo=timezone.utc))
    assert len(posts) == 1
    assert posts[0].text == 'New\n\nhttps://example.com/new'
    assert posts[0].media[0].url == 'https://example.com/preview.png'
    assert posts[0].media[0].link_title == 'New & interesting'
    with engine.begin() as connection:
        save_posts(connection, account.id, posts)
    assert load_posts(engine, account.id) == posts


@pytest.mark.asyncio
async def test_bluesky_rss_card_uses_png_blob():
    class Session:
        async def post_bytes(self, method, raw, content_type):
            assert method == 'com.atproto.repo.uploadBlob'
            assert content_type == 'image/png'
            return {'blob': {'$type': 'blob', 'ref': {'$link': 'image-cid'}}}

    media = MediaItem('https://example.com/image', 'photo', link_url='https://example.com/post',
                      link_title='Title', link_description='Description')
    embed = await _build_embed(Session(), [media], [b'\x89PNG\r\n\x1a\nimage'])
    assert embed['$type'] == 'app.bsky.embed.external'
    assert embed['external']['uri'] == 'https://example.com/post'
    assert embed['external']['title'] == 'Title'
    assert embed['external']['thumb']['ref']['$link'] == 'image-cid'
