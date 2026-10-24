import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx

from apis.types import MediaItem, PublishResult
from config import (
    NETWORK_LIMITS,
    NETWORKS,
    WATCH_INITIAL_LOOKBACK_HOURS,
    WATCH_OVERLAP_HOURS,
)
from db.accounts import create_account
from db.sync_state import (
    get_dest_post_id,
    get_last_synced_at,
    get_mirrored_post_ids,
    is_synced,
    record_mirrored_post,
    set_last_synced_at,
)
from sync.engine import run_sync


@pytest.fixture(autouse=True)
def http_mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def adapters(monkeypatch):
    adapters = {}
    for network in NETWORKS:
        adapter = SimpleNamespace(
            fetch_posts=AsyncMock(return_value=[]),
            download_media=AsyncMock(side_effect=lambda media, *args: media.url.encode()),
            publish_outbound=AsyncMock(),
        )
        # IDs and reply refs deliberately differ: the engine must respect both contracts.
        def published(*args, network=network, adapter=adapter, **kwargs):
            number = adapter.publish_outbound.await_count
            return PublishResult(f"{network}-{number}", f"reply-{network}-{number}")

        adapter.publish_outbound.side_effect = published
        module = import_module(f"apis.{network}")
        for name in ("fetch_posts", "download_media", "publish_outbound"):
            monkeypatch.setattr(module, name, getattr(adapter, name))
        adapters[network] = adapter
    return adapters


async def test_mesh_routes_by_account_and_keeps_same_source_ids_independent(
    engine, adapters, post_factory
):
    personal = create_account(engine, "twitter", "personal", "alice")
    work = create_account(engine, "twitter", "work", "bob")
    telegram = create_account(engine, "telegram", "main", "channel")
    rss = create_account(engine, "rss", "blog", "feed")
    instagram = create_account(engine, "instagram", "photos", "ig")
    adapters["twitter"].fetch_posts.side_effect = lambda db, account_id, **kwargs: (
        [post_factory("1", text="Personal post")] if account_id == personal.id else []
    )
    adapters["rss"].fetch_posts.return_value = [post_factory("1", text="Blog post")]
    adapters["instagram"].fetch_posts.return_value = [post_factory("1", text="Photo post")]

    assert await run_sync(engine) == 8
    assert await run_sync(engine) == 0

    for source in (personal, rss, instagram):
        for dest in (personal, work, telegram):
            assert is_synced(engine, source.id, "1", dest.id) == (source.id != dest.id)
        assert not is_synced(engine, source.id, "1", rss.id)
        assert not is_synced(engine, source.id, "1", instagram.id)

    assert adapters["twitter"].publish_outbound.await_count == 5
    assert adapters["telegram"].publish_outbound.await_count == 3
    adapters["rss"].publish_outbound.assert_not_awaited()
    adapters["instagram"].publish_outbound.assert_not_awaited()
    published_texts = [call.args[2].text for call in adapters["telegram"].publish_outbound.await_args_list]
    assert sorted(published_texts) == ["Blog post", "Personal post", "Photo post"]


async def test_sync_created_posts_never_echo_back(engine, adapters, post_factory):
    source = create_account(engine, "twitter", "main", "alice")
    dest = create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1")]

    assert await run_sync(engine) == 1
    mirrored_id = get_dest_post_id(engine, source.id, "1", dest.id)
    assert mirrored_id in get_mirrored_post_ids(engine, dest.id)
    adapters["telegram"].fetch_posts.return_value = [post_factory(mirrored_id)]

    assert await run_sync(engine) == 0
    adapters["twitter"].publish_outbound.assert_not_awaited()
    assert adapters["telegram"].publish_outbound.await_count == 1


@pytest.mark.parametrize("network", ["twitter", "mastodon", "threads", "bluesky"])
async def test_only_original_posts_and_connected_own_replies_are_published(
    engine, adapters, post_factory, network
):
    source = create_account(engine, network, "main", "author")
    dest = create_account(engine, "telegram", "main", "channel")
    root = post_factory("1", text="Root")
    own_reply = post_factory("2", text="Own reply", conversation_id="1", in_reply_to_id="1")
    other_reply = post_factory("3", text="To someone else", conversation_id="1", in_reply_to_id="1")
    other_reply.in_reply_to_user_id = "another-author"
    orphan = post_factory("4", conversation_id="missing", in_reply_to_id="missing")
    orphan_tail = post_factory("5", conversation_id="missing", in_reply_to_id="4")
    local = post_factory("6", text="Keep here /x  \n")
    mirrored = post_factory("7", text="Mirrored elsewhere")
    record_mirrored_post(engine, source.id, mirrored.id)
    adapters[network].fetch_posts.return_value = [
        orphan_tail, other_reply, own_reply, root, orphan, local, mirrored,
    ]

    assert await run_sync(engine) == 2

    sent = adapters["telegram"].publish_outbound.await_args.args[2]
    assert sent.text == "Root\n\nOwn reply"
    assert sent.source_post_ids == ["1", "2"]
    for post_id in ("3", "4", "5", "6", "7"):
        assert not is_synced(engine, source.id, post_id, dest.id)


async def test_destination_failure_retries_only_unsynced_destinations(
    engine, adapters, post_factory
):
    source = create_account(engine, "rss", "blog", "feed")
    failing = create_account(engine, "telegram", "a-failing", "one")
    healthy = create_account(engine, "telegram", "b-healthy", "two")
    adapters["rss"].fetch_posts.return_value = [post_factory("1")]
    attempts = []

    async def publish(db, account_id, outbound, media_bytes, **kwargs):
        attempts.append(account_id)
        if account_id == failing.id and attempts.count(failing.id) == 1:
            raise RuntimeError("Temporary network failure")
        return PublishResult(f"posted-{account_id}")

    adapters["telegram"].publish_outbound.side_effect = publish

    assert await run_sync(engine) == 1
    assert not is_synced(engine, source.id, "1", failing.id)
    assert get_mirrored_post_ids(engine, failing.id) == set()
    assert is_synced(engine, source.id, "1", healthy.id)

    assert await run_sync(engine) == 1
    assert attempts.count(healthy.id) == 1
    assert attempts.count(failing.id) == 2
    assert is_synced(engine, source.id, "1", failing.id)


async def test_failed_source_does_not_block_other_sources_or_advance_its_watermark(
    engine, adapters, post_factory
):
    broken = create_account(engine, "bluesky", "main", "did:broken")
    healthy = create_account(engine, "rss", "main", "feed")
    dest = create_account(engine, "telegram", "main", "channel")
    adapters["bluesky"].fetch_posts.side_effect = RuntimeError("Unavailable")
    adapters["rss"].fetch_posts.return_value = [post_factory("1")]

    assert await run_sync(engine) == 2

    assert get_last_synced_at(engine, broken.id) is None
    assert get_last_synced_at(engine, healthy.id) is not None
    assert is_synced(engine, healthy.id, "1", dest.id)


@pytest.mark.parametrize("network", ["telegram", "bluesky"])
async def test_split_thread_chains_messages_and_later_replies_resume_from_last_destination(
    engine, adapters, post_factory, monkeypatch, network
):
    source = create_account(engine, "twitter", "main", "author")
    dest = create_account(engine, network, "main", "remote")
    root = post_factory("1", text="a" * (NETWORK_LIMITS[network].max_text + 100))
    reply = post_factory("2", text="Later reply", conversation_id="1", in_reply_to_id="1")
    resolver = AsyncMock(return_value="resolved-reply-ref")
    monkeypatch.setattr("apis.bluesky.resolve_reply_target", resolver)
    adapters["twitter"].fetch_posts.return_value = [root]

    assert await run_sync(engine) == 1
    calls = adapters[network].publish_outbound.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["reply_to"] is None
    assert calls[1].kwargs["reply_to"] == f"reply-{network}-1"
    assert get_dest_post_id(engine, source.id, "1", dest.id) == f"{network}-2"
    assert get_mirrored_post_ids(engine, dest.id) == {f"{network}-1", f"{network}-2"}

    adapters["twitter"].fetch_posts.return_value = [reply, root]
    assert await run_sync(engine) == 1
    last = adapters[network].publish_outbound.await_args
    assert last.args[2].text == "Later reply"
    assert last.kwargs["reply_to"] == (
        "resolved-reply-ref" if network == "bluesky" else "telegram-2"
    )
    if network == "bluesky":
        resolver.assert_awaited_once_with(engine, dest.id, "bluesky-2")
    assert get_dest_post_id(engine, source.id, "2", dest.id) == f"{network}-3"


@pytest.mark.parametrize(
    "root_age,enforce_min_age,expected",
    [(60, True, 2), (5, True, 0), (5, False, 2)],
)
async def test_age_gate_holds_whole_thread_but_allows_young_replies_to_ready_root(
    engine, adapters, post_factory, root_age, enforce_min_age, expected
):
    create_account(engine, "twitter", "main", "author")
    create_account(engine, "telegram", "main", "channel")
    now = datetime.now(timezone.utc)
    root = post_factory("1", created_at=now - timedelta(minutes=root_age))
    reply = post_factory(
        "2", created_at=now - timedelta(minutes=1), conversation_id="1", in_reply_to_id="1"
    )
    adapters["twitter"].fetch_posts.return_value = [reply, root]

    assert await run_sync(engine, enforce_min_age=enforce_min_age) == expected
    assert adapters["telegram"].publish_outbound.await_count == bool(expected)


@pytest.mark.parametrize("mode", [
    "initial",
    "overlap",
    "backfill",
])
async def test_fetch_window_uses_initial_lookback_overlap_or_explicit_backfill(
    engine, adapters, mode
):
    source = create_account(engine, "twitter", "main", "author")
    create_account(engine, "telegram", "main", "channel")
    before = datetime.now(timezone.utc)
    watermark = before - timedelta(hours=1)
    since = before - timedelta(days=30) if mode == "backfill" else None
    if mode != "initial":
        set_last_synced_at(engine, source.id, watermark)

    assert await run_sync(engine, since=since) == 0

    fetch = adapters["twitter"].fetch_posts.await_args
    if mode == "initial":
        after = datetime.now(timezone.utc)
        assert before - timedelta(hours=WATCH_INITIAL_LOOKBACK_HOURS) <= fetch.kwargs["since"]
        assert fetch.kwargs["since"] <= after - timedelta(hours=WATCH_INITIAL_LOOKBACK_HOURS)
    else:
        assert fetch.kwargs["since"] == (
            since if mode == "backfill" else watermark - timedelta(hours=WATCH_OVERLAP_HOURS)
        )
    if mode == "backfill":
        assert fetch.kwargs["max_pages"] is None
    assert get_last_synced_at(engine, source.id).replace(tzinfo=timezone.utc) >= before


@pytest.mark.parametrize("backfill", [False, True])
async def test_only_explicit_backfill_adds_publish_delays(
    engine, adapters, post_factory, monkeypatch, backfill
):
    create_account(engine, "twitter", "main", "author")
    create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="a" * 5000, created_at=datetime.now(timezone.utc))]
    sleep = AsyncMock()
    monkeypatch.setattr("sync.engine.asyncio.sleep", sleep)

    since = datetime.now(timezone.utc) - timedelta(days=1) if backfill else None
    assert await run_sync(engine, since=since, enforce_min_age=False) == 1
    assert bool(sleep.await_count) == backfill
    assert all(call.args[0] > 0 for call in sleep.await_args_list)


@pytest.mark.parametrize(
    "media_types",
    [
        pytest.param(["photo"] * 6, id="photo-albums"),
        pytest.param(
            ["video", "photo"], id="mixed-media",
        ),
    ],
)
async def test_published_media_stays_paired_with_its_original_bytes(
    engine, adapters, post_factory, media_types
):
    create_account(engine, "twitter", "main", "author")
    create_account(engine, "mastodon", "main", "remote")
    media = [MediaItem(f"https://media.test/{i}", kind) for i, kind in enumerate(media_types)]
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="Caption", media=media)]

    assert await run_sync(engine) == 1

    published_media = []
    for call in adapters["mastodon"].publish_outbound.await_args_list:
        outbound, raw = call.args[2:4]
        assert len(outbound.media) == len(raw)
        for item, data in zip(outbound.media, raw):
            assert data == item.url.encode()
        published_media.extend(outbound.media)
    assert sorted(item.url for item in published_media) == sorted(item.url for item in media)


async def test_threads_destination_uses_urls_without_downloading_media(
    engine, adapters, post_factory
):
    create_account(engine, "twitter", "main", "author")
    create_account(engine, "threads", "main", "remote")
    photo = MediaItem("https://media.test/photo.jpg", "photo")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", media=[photo])]
    adapters["twitter"].download_media.side_effect = RuntimeError("Must not download")

    assert await run_sync(engine) == 1
    sent = adapters["threads"].publish_outbound.await_args
    assert sent.args[2].media == [photo]
    assert sent.args[3] == []
    adapters["twitter"].download_media.assert_not_awaited()


async def test_links_are_expanded_before_publishing(engine, adapters, post_factory, http_mock):
    create_account(engine, "twitter", "main", "author")
    create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="Read https://t.co/short")]
    http_mock.get("https://t.co/short").respond(302, headers={"Location": "https://article.test/post"})
    http_mock.get("https://article.test/post").respond(200)

    assert await run_sync(engine) == 1
    assert adapters["telegram"].publish_outbound.await_args.args[2].text == "Read https://article.test/post"


async def test_retry_after_partial_publish_does_not_duplicate_successful_parts(
    engine, adapters, post_factory
):
    create_account(engine, "twitter", "main", "author")
    create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="a" * 4096 + "b" * 50)]
    successful_texts = []
    failed_once = False

    async def publish(db, account_id, outbound, media_bytes, **kwargs):
        nonlocal failed_once
        if outbound.text.startswith("b") and not failed_once:
            failed_once = True
            raise RuntimeError("Second message failed")
        successful_texts.append(outbound.text)
        return PublishResult(f"posted-{len(successful_texts)}")

    adapters["telegram"].publish_outbound.side_effect = publish
    await run_sync(engine)
    await run_sync(engine)

    assert len(successful_texts) == len(set(successful_texts)) == 2


@pytest.mark.parametrize("source_only_accounts", [False, True])
async def test_no_publishable_routes_does_no_network_work(engine, adapters, source_only_accounts):
    if source_only_accounts:
        create_account(engine, "rss", "blog", "feed")
        create_account(engine, "instagram", "photos", "ig")

    assert await run_sync(engine) == 0
    for adapter in adapters.values():
        adapter.fetch_posts.assert_not_awaited()
        adapter.publish_outbound.assert_not_awaited()


async def test_failed_media_download_leaves_post_pending_for_retry(engine, adapters, post_factory):
    source = create_account(engine, "twitter", "main", "author")
    dest = create_account(engine, "telegram", "main", "channel")
    photo = MediaItem("https://media.test/photo.jpg", "photo")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", media=[photo])]
    adapters["twitter"].download_media.side_effect = [RuntimeError("Download failed"), b"photo"]

    assert await run_sync(engine) == 0
    assert not is_synced(engine, source.id, "1", dest.id)
    adapters["telegram"].publish_outbound.assert_not_awaited()

    assert await run_sync(engine) == 1
    assert adapters["telegram"].publish_outbound.await_args.args[3] == [b"photo"]
    assert is_synced(engine, source.id, "1", dest.id)


async def test_instagram_story_batch_preserves_slides_with_one_caption(engine, adapters, post_factory):
    source = create_account(engine, "instagram", "main", "ig")
    dest = create_account(engine, "telegram", "main", "channel")
    first = MediaItem("https://media.test/first.jpg", "photo")
    second = MediaItem("https://media.test/second.jpg", "photo")
    adapters["instagram"].fetch_posts.return_value = [
        post_factory("story_1", text="Story caption", conversation_id="stories", media=[first], is_thread_root=True),
        post_factory("story_2", text="Second slide caption", conversation_id="stories", media=[second]),
    ]

    assert await run_sync(engine) == 2

    sent = adapters["telegram"].publish_outbound.await_args
    assert sent.args[2].text == "Story caption"
    assert sent.args[2].media == [first, second]
    assert sent.args[3] == [first.url.encode(), second.url.encode()]
    assert get_dest_post_id(engine, source.id, "story_1", dest.id) == get_dest_post_id(engine, source.id, "story_2", dest.id)


async def test_watch_bypasses_age_gate_on_startup_then_holds_new_young_posts(
    engine, adapters, post_factory, monkeypatch
):
    from main import watch_mode

    source = create_account(engine, "twitter", "main", "author")
    dest = create_account(engine, "telegram", "main", "channel")
    now = datetime.now(timezone.utc)
    startup_post = post_factory("1", text="Available at startup", created_at=now)
    later_post = post_factory("2", text="New editable post", created_at=now)
    adapters["twitter"].fetch_posts.side_effect = [[startup_post], [startup_post, later_post]]
    monkeypatch.setattr("main.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))

    with pytest.raises(asyncio.CancelledError):
        await watch_mode(engine)

    assert is_synced(engine, source.id, "1", dest.id)
    assert not is_synced(engine, source.id, "2", dest.id)
    assert adapters["telegram"].publish_outbound.await_count == 1
    assert adapters["telegram"].publish_outbound.await_args.args[2].text == "Available at startup"


async def test_projects_route_only_within_their_own_matrix(engine, adapters, post_factory):
    from db.projects import create_project, get_project
    project = create_project(engine, "Work")
    blog = create_account(engine, "rss", "feed", "blog")
    personal = create_account(engine, "twitter", "main", "alice")
    work = create_account(engine, "rss", "feed", "work", project_id=project.id)
    company = create_account(engine, "twitter", "main", "company", project_id=project.id)
    colleague = create_account(engine, "twitter", "colleague", "bob", project_id=project.id)
    adapters["rss"].fetch_posts.side_effect = lambda db, account_id, **kwargs: [post_factory("1", text=str(account_id))]

    assert await run_sync(engine) == 3

    sent = [(call.args[1], call.args[2].text) for call in adapters["twitter"].publish_outbound.await_args_list]
    assert sent == [(personal.id, str(blog.id)), (colleague.id, str(work.id)), (company.id, str(work.id))]
    assert is_synced(engine, blog.id, "1", personal.id)
    assert not is_synced(engine, blog.id, "1", company.id)
    assert not is_synced(engine, work.id, "1", personal.id)
    assert get_project(engine, 1).last_sync_count == 1
    assert get_project(engine, project.id).last_sync_count == 2


async def test_shared_destinations_receive_only_their_projects_and_never_act_as_sources(engine, adapters, post_factory):
    from db.projects import create_project
    project = create_project(engine, "Work")
    first = create_account(engine, "twitter", "main", "same-person", sync_role="destination_only")
    second = create_account(engine, "twitter", "main", "same-person", project_id=project.id, sync_role="destination_only")
    first_source = create_account(engine, "telegram", "main", "personal", sync_role="source_only")
    second_source = create_account(engine, "telegram", "main", "work", project_id=project.id, sync_role="source_only")
    adapters["telegram"].fetch_posts.side_effect = lambda db, account_id, **kwargs: [post_factory(str(account_id))]
    adapters["twitter"].fetch_posts.return_value = [post_factory("personal-twitter-post")]

    assert await run_sync(engine) == 2
    assert await run_sync(engine) == 0

    assert is_synced(engine, first_source.id, str(first_source.id), first.id)
    assert is_synced(engine, second_source.id, str(second_source.id), second.id)
    assert not is_synced(engine, first_source.id, str(first_source.id), second.id)
    adapters["twitter"].fetch_posts.assert_not_awaited()
    adapters["telegram"].publish_outbound.assert_not_awaited()
    assert get_mirrored_post_ids(engine, first.id) == get_mirrored_post_ids(engine, second.id)


async def test_mirror_created_in_first_project_is_filtered_in_second_during_same_cycle(engine, adapters, post_factory):
    from db.projects import create_project
    project = create_project(engine, "Work")
    create_account(engine, "rss", "feed", "blog")
    shared = create_account(engine, "telegram", "main", "same-channel", sync_role="destination_only")
    other = create_account(engine, "telegram", "main", "same-channel", project_id=project.id, sync_role="destination_only")
    create_account(engine, "twitter", "work", "company", project_id=project.id)
    adapters["rss"].fetch_posts.return_value = [post_factory("original")]
    adapters["telegram"].fetch_posts.side_effect = lambda db, account_id, **kwargs: [post_factory("telegram-1")] if account_id == other.id else []

    assert await run_sync(engine) == 1

    adapters["twitter"].publish_outbound.assert_not_awaited()
    assert get_mirrored_post_ids(engine, shared.id) == get_mirrored_post_ids(engine, other.id)
    adapters["telegram"].fetch_posts.assert_not_awaited()


async def test_project_selection_and_disabled_projects_do_no_unselected_work(engine, adapters, post_factory):
    from db.projects import configure_project, create_project, get_project
    project = create_project(engine, "Work")
    default_source = create_account(engine, "rss", "feed", "blog")
    create_account(engine, "telegram", "main", "personal")
    work_source = create_account(engine, "rss", "feed", "work", project_id=project.id)
    create_account(engine, "telegram", "main", "company", project_id=project.id)
    adapters["rss"].fetch_posts.return_value = [post_factory("1")]

    assert await run_sync(engine, project_id=project.id) == 1
    assert [call.args[1] for call in adapters["rss"].fetch_posts.await_args_list] == [work_source.id]
    assert get_last_synced_at(engine, default_source.id) is None
    assert get_project(engine, 1).last_sync_started_at is None
    previous = get_project(engine, project.id)
    configure_project(engine, project.id, enabled=False)
    assert await run_sync(engine, project_id=project.id) == 0
    assert get_project(engine, project.id).last_sync_finished_at == previous.last_sync_finished_at
    assert await run_sync(engine) == 1
    assert adapters["rss"].fetch_posts.await_args.args[1] == default_source.id


async def test_project_timing_settings_control_age_windows_and_backfill_delay(engine, adapters, post_factory, monkeypatch):
    from db.projects import configure_project, create_project
    project = create_project(engine, "Work")
    configure_project(engine, project.id, min_age_minutes=60, overlap_hours=2, initial_lookback_hours=12, backfill_delay_seconds=0.25)
    source = create_account(engine, "rss", "feed", "work", project_id=project.id)
    dest = create_account(engine, "telegram", "main", "channel", project_id=project.id)
    now = datetime.now(timezone.utc)
    adapters["rss"].fetch_posts.return_value = [post_factory("1", created_at=now - timedelta(minutes=30))]

    assert await run_sync(engine, project_id=project.id) == 0
    requested_since = adapters["rss"].fetch_posts.await_args.kwargs["since"]
    assert abs((requested_since - (now - timedelta(hours=12))).total_seconds()) < 5
    last_fetch = get_last_synced_at(engine, source.id)
    assert await run_sync(engine, project_id=project.id) == 0
    assert adapters["rss"].fetch_posts.await_args.kwargs["since"] == last_fetch - timedelta(hours=2)

    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert await run_sync(engine, since=now - timedelta(hours=1), project_id=project.id) == 1
    sleep.assert_awaited_once_with(0.25)
    assert is_synced(engine, source.id, "1", dest.id)


async def test_project_failure_metadata_and_pending_delivery_stay_independent(engine, adapters, post_factory):
    from db.deliveries import load_deliveries
    from db.projects import create_project, get_project
    project = create_project(engine, "Work")
    first_source = create_account(engine, "rss", "feed", "blog")
    broken = create_account(engine, "telegram", "main", "personal")
    second_source = create_account(engine, "rss", "feed", "work", project_id=project.id)
    healthy = create_account(engine, "telegram", "main", "company", project_id=project.id)
    adapters["rss"].fetch_posts.return_value = [post_factory("1")]

    async def publish(db, account_id, *args, **kwargs):
        if account_id == broken.id:
            raise RuntimeError("Unavailable")
        return PublishResult("success")

    adapters["telegram"].publish_outbound.side_effect = publish
    assert await run_sync(engine) == 1
    assert get_project(engine, 1).last_synced_at is None
    assert get_project(engine, 1).last_sync_finished_at is not None
    assert get_project(engine, 1).last_sync_error_count == 1
    assert get_project(engine, project.id).last_synced_at is not None
    assert get_project(engine, project.id).last_sync_error_count == 0
    assert is_synced(engine, second_source.id, "1", healthy.id)
    assert len(load_deliveries(engine, first_source.id)) == 1

    adapters["telegram"].publish_outbound.side_effect = lambda *args, **kwargs: PublishResult("recovered")
    assert await run_sync(engine, project_id=project.id) == 0
    assert len(load_deliveries(engine, first_source.id)) == 1
    assert await run_sync(engine, project_id=1) == 1
    assert get_project(engine, 1).last_synced_at is not None
    assert get_project(engine, 1).last_sync_error_count == 0
    assert load_deliveries(engine, first_source.id) == []


async def test_pending_post_survives_empty_fetch_and_restart(engine, adapters, post_factory):
    from pathlib import Path
    from db.connection import get_engine

    source = create_account(engine, "twitter", "main", "author")
    dest = create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1")]
    adapters["telegram"].publish_outbound.side_effect = RuntimeError("Unavailable")
    assert await run_sync(engine) == 0

    reopened = get_engine(Path(engine.url.database))
    adapters["twitter"].fetch_posts.return_value = []
    adapters["telegram"].publish_outbound.side_effect = lambda *args, **kwargs: PublishResult("recovered")
    try:
        assert await run_sync(reopened) == 1
        assert get_dest_post_id(reopened, source.id, "1", dest.id) == "recovered"
    finally:
        reopened.dispose()


async def test_partial_batch_resumes_its_saved_content_after_source_edits(engine, adapters, post_factory):
    from pathlib import Path
    from db.connection import get_engine

    source = create_account(engine, "twitter", "main", "author")
    dest = create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="a" * 4096 + "original tail")]
    adapters["telegram"].publish_outbound.side_effect = [PublishResult("first", "reply-first"), RuntimeError("Unavailable")]
    assert await run_sync(engine) == 0

    reopened = get_engine(Path(engine.url.database))
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="Edited content")]
    adapters["telegram"].publish_outbound.side_effect = lambda *args, **kwargs: PublishResult("last")
    try:
        assert await run_sync(reopened) == 1
        resumed = adapters["telegram"].publish_outbound.await_args
        assert resumed.args[2].text == "original tail"
        assert resumed.kwargs["reply_to"] == "reply-first"
        assert get_dest_post_id(reopened, source.id, "1", dest.id) == "last"
    finally:
        reopened.dispose()


async def test_failed_fetch_still_delivers_saved_posts(engine, adapters, post_factory):
    source = create_account(engine, "twitter", "main", "author")
    dest = create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1")]
    adapters["telegram"].publish_outbound.side_effect = RuntimeError("Unavailable")
    assert await run_sync(engine) == 0
    watermark = get_last_synced_at(engine, source.id)

    adapters["twitter"].fetch_posts.side_effect = RuntimeError("Fetch failed")
    adapters["telegram"].publish_outbound.side_effect = lambda *args, **kwargs: PublishResult("recovered")
    assert await run_sync(engine) == 1
    assert get_dest_post_id(engine, source.id, "1", dest.id) == "recovered"
    assert get_last_synced_at(engine, source.id) == watermark


@pytest.mark.parametrize("source_role", ["source_only", "destination_only", "source_and_destination"])
@pytest.mark.parametrize("dest_role", ["source_only", "destination_only", "source_and_destination"])
async def test_routing_obeys_each_connection_role(engine, adapters, post_factory, source_role, dest_role):
    first = create_account(engine, "twitter", "main", "alice", sync_role=source_role)
    second = create_account(engine, "telegram", "main", "channel", sync_role=dest_role)
    adapters["twitter"].fetch_posts.return_value = [post_factory("twitter-original")]
    adapters["telegram"].fetch_posts.return_value = [post_factory("telegram-original")]
    forward = source_role != "destination_only" and dest_role != "source_only"
    backward = dest_role != "destination_only" and source_role != "source_only"

    assert await run_sync(engine) == int(forward) + int(backward)
    assert await run_sync(engine) == 0

    assert is_synced(engine, first.id, "twitter-original", second.id) == forward
    assert is_synced(engine, second.id, "telegram-original", first.id) == backward
    if source_role == "destination_only":
        adapters["twitter"].fetch_posts.assert_not_awaited()
    if dest_role == "source_only":
        adapters["telegram"].publish_outbound.assert_not_awaited()


@pytest.mark.parametrize("changed_end", ["source", "destination"])
async def test_role_changes_stop_pending_deliveries_without_republishing_completed_parts(engine, adapters, post_factory, changed_end):
    from db.accounts import set_account_role
    source = create_account(engine, "twitter", "main", "alice")
    dest = create_account(engine, "telegram", "main", "channel")
    adapters["twitter"].fetch_posts.return_value = [post_factory("1", text="a" * 4096 + "tail")]
    adapters["telegram"].publish_outbound.side_effect = [PublishResult("first", "reply-first"), RuntimeError("Unavailable")]
    assert await run_sync(engine) == 0
    changed = source if changed_end == "source" else dest
    set_account_role(engine, changed.id, "destination_only" if changed_end == "source" else "source_only")
    adapters["telegram"].publish_outbound.side_effect = lambda *args, **kwargs: PublishResult("last")

    assert await run_sync(engine) == 0
    assert adapters["telegram"].publish_outbound.await_count == 2

    set_account_role(engine, changed.id, "source_and_destination")
    assert await run_sync(engine) == 1
    assert adapters["telegram"].publish_outbound.await_count == 3
    assert adapters["telegram"].publish_outbound.await_args.kwargs["reply_to"] == "reply-first"


async def test_legacy_shared_sources_fail_closed_even_with_cached_posts(engine, adapters, post_factory):
    from sqlalchemy import update
    from db.posts import save_posts
    from db.projects import create_project, get_project
    from db.schema import accounts
    project = create_project(engine, "Work")
    first = create_account(engine, "twitter", "main", "alice")
    second = create_account(engine, "twitter", "main", "bob", project_id=project.id)
    create_account(engine, "telegram", "main", "personal")
    create_account(engine, "telegram", "main", "work", project_id=project.id)
    with engine.begin() as connection:
        connection.execute(update(accounts).where(accounts.c.id == second.id).values(remote_id="alice"))
        save_posts(connection, first.id, [post_factory("cached")])
        save_posts(connection, second.id, [post_factory("cached")])

    assert await run_sync(engine) == 0

    adapters["twitter"].fetch_posts.assert_not_awaited()
    adapters["telegram"].publish_outbound.assert_not_awaited()
    assert get_project(engine, 1).last_sync_error_count == 1
    assert get_project(engine, project.id).last_sync_error_count == 1
