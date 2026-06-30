from xml.etree import ElementTree as ET

from apis.rss import (
    _item_text,
    _parse_atom,
    _parse_feed,
    _parse_rss,
    _stable_post_id,
    _strip_html,
)


def test_stable_post_id_uses_short_guid():
    assert _stable_post_id("abc-123", None) == "abc-123"


def test_stable_post_id_hashes_long_values():
    long_id = "x" * 100
    result = _stable_post_id(long_id)
    assert len(result) == 64
    assert result == _stable_post_id(long_id)


def test_stable_post_id_raises_without_candidates():
    try:
        _stable_post_id(None, "")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_strip_html():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_item_text_includes_link():
    text = _item_text("Title", "Summary", "https://example.com/post")
    assert "Title" in text
    assert "Summary" in text
    assert "https://example.com/post" in text


def test_parse_rss_feed():
    xml = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Test Feed</title>
        <item>
          <title>Hello</title>
          <link>https://example.com/1</link>
          <guid>guid-1</guid>
          <description><![CDATA[<p>World</p>]]></description>
          <pubDate>Mon, 01 Jun 2026 10:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>"""
    posts = _parse_rss(ET.fromstring(xml), "https://example.com/feed.xml")
    assert len(posts) == 1
    assert posts[0].id == "guid-1"
    assert "Hello" in posts[0].text
    assert "World" in posts[0].text
    assert posts[0].conversation_id == posts[0].id


def test_parse_atom_feed():
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom Feed</title>
      <entry>
        <title>Entry</title>
        <id>entry-1</id>
        <link href="https://example.com/atom/1" rel="alternate"/>
        <summary>Summary text</summary>
        <published>2026-06-01T10:00:00Z</published>
      </entry>
    </feed>"""
    posts = _parse_atom(ET.fromstring(xml), "https://example.com/atom.xml")
    assert len(posts) == 1
    assert posts[0].id == "entry-1"
    assert "Entry" in posts[0].text


def test_parse_feed_detects_format():
    rss_xml = """<rss version="2.0"><channel><title>T</title></channel></rss>"""
    assert _parse_feed(rss_xml, "https://example.com/rss") == []
