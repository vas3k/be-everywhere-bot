from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from db.accounts import create_account, get_account, get_all_credentials, list_accounts, set_credentials
from db.sync_state import get_dest_post_id, mark_synced
from utils.meta_tokens import FACEBOOK_TOKEN, INSTAGRAM_TOKEN, THREADS_TOKEN


@pytest.fixture(autouse=True)
def http_mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture(params=["twitter", "telegram", "mastodon", "threads", "bluesky", "instagram", "linkedin", "rss"])
def auth_case(request, monkeypatch, http_mock):
    network = request.param
    cases = {
        "twitter": {
            "inputs": ["token", "@alice"],
            "url": "https://api.x.com/2/users/by/username/alice",
            "payload": {"data": {"id": "remote-1", "username": "alice"}},
            "credentials": {"bearer_token": "token", "user_id": "remote-1", "username": "alice"},
            "remote_id": "remote-1",
        },
        "telegram": {
            "inputs": ["token", "@channel"],
            "url": r"https://api\.telegram\.org/bot[^/]*/getMe",
            "payload": {"ok": True, "result": {"id": 123, "username": "bot"}},
            "credentials": {"bot_token": "token", "channel_id": "@channel", "update_offset": "0"},
            "remote_id": "-100123",
        },
        "mastodon": {
            "inputs": ["https://mastodon.test/", "token"],
            "url": "https://mastodon.test/api/v1/accounts/verify_credentials",
            "payload": {"id": "remote-1", "username": "alice"},
            "credentials": {"instance_url": "https://mastodon.test", "access_token": "token", "account_id": "remote-1", "username": "alice"},
            "remote_id": "remote-1",
        },
        "threads": {
            "inputs": ["token", "secret", ""],
            "url": "https://graph.threads.net/v1.0/me",
            "payload": {"id": "remote-1", "username": "alice"},
            "credentials": {"access_token": "long-lived", "app_secret": "secret", "user_id": "remote-1", "username": "alice"},
            "remote_id": "remote-1",
        },
        "bluesky": {
            "inputs": ["@alice.test", "app-password", "https://pds.test"],
            "url": "https://pds.test/xrpc/com.atproto.server.createSession",
            "payload": {"did": "did:plc:alice", "handle": "alice.test", "accessJwt": "access", "refreshJwt": "refresh"},
            "credentials": {"handle": "alice.test", "did": "did:plc:alice", "access_jwt": "access", "refresh_jwt": "refresh", "pds_url": "https://pds.test"},
            "remote_id": "did:plc:alice",
        },
        "instagram": {
            "inputs": ["token", "secret", ""],
            "url": "https://graph.instagram.com/v21.0/me",
            "payload": {"user_id": "remote-1", "username": "alice"},
            "credentials": {"access_token": "long-lived", "app_secret": "secret", "user_id": "remote-1", "username": "alice", "token_kind": "instagram"},
            "remote_id": "remote-1",
        },
        "linkedin": {
            "inputs": ["token", ""],
            "url": "https://api.linkedin.com/v2/userinfo",
            "payload": {"sub": "remote-1", "name": "Alice"},
            "credentials": {"access_token": "token", "person_urn": "urn:li:person:remote-1", "display_name": "Alice"},
            "remote_id": "remote-1",
        },
        "rss": {
            "inputs": ["https://feed.test/rss"],
            "url": "https://feed.test/rss",
            "payload": None,
            "credentials": {"feed_url": "https://feed.test/rss"},
            "remote_id": "https://feed.test/rss",
        },
    }
    case = SimpleNamespace(network=network, module=import_module(f"apis.{network}"), **cases[network])
    values = iter(case.inputs)
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))
    if network == "bluesky":
        case.route = http_mock.post(case.url).respond(200, json=case.payload)
    elif network == "telegram":
        case.route = http_mock.get(url__regex=case.url).respond(200, json=case.payload)
        http_mock.get("https://api.telegram.org/bottoken/getChat").respond(200, json={"result": {"id": -100123, "username": "channel"}})
    elif network == "rss":
        case.route = http_mock.get(case.url).respond(200, text="<rss><channel><title>Blog</title></channel></rss>")
    else:
        case.route = http_mock.get(case.url).respond(200, json=case.payload)
    if network in ("threads", "instagram"):
        config = THREADS_TOKEN if network == "threads" else INSTAGRAM_TOKEN
        http_mock.get(config.exchange.url).respond(200, json={"access_token": "long-lived", "expires_in": 60 * 86400})
    if network == "instagram":
        http_mock.get("https://graph.facebook.com/v21.0/me/accounts").respond(401, json={"error": {"message": "Rejected"}})
    return case


@pytest.mark.parametrize("input_mode", ["cli", "form"])
async def test_authentication_stores_verified_identity_and_usable_credentials(engine, auth_case, monkeypatch, input_mode):
    kwargs = {}
    if input_mode == "form":
        from tui.forms import AUTH_FIELDS
        keys = [key for key, _ in AUTH_FIELDS[auth_case.network] if key != "app_id"]
        kwargs["credentials"] = dict(zip(keys, auth_case.inputs, strict=True))

        def unexpected_prompt(prompt):
            raise AssertionError("Form authentication must not prompt on stdin")

        monkeypatch.setattr("builtins.input", unexpected_prompt)
    account = await auth_case.module.authenticate(engine, label="personal", **kwargs)

    assert list_accounts(engine) == [get_account(engine, account.id)]
    stored = get_account(engine, account.id)
    assert (stored.network, stored.label, stored.remote_id) == (
        auth_case.network, "personal", auth_case.remote_id
    )
    credentials = get_all_credentials(engine, account.id)
    assert credentials.items() >= auth_case.credentials.items()
    assert "app_password" not in credentials
    assert "password" not in credentials
    request = auth_case.route.calls[0].request
    if auth_case.network in ("twitter", "mastodon", "linkedin"):
        assert request.headers["Authorization"] == "Bearer token"
    elif auth_case.network == "threads":
        assert request.url.params["access_token"] == "long-lived"
    elif auth_case.network == "instagram":
        assert request.url.params["access_token"] == "token"
    elif auth_case.network == "bluesky":
        assert json.loads(request.content) == {"identifier": "alice.test", "password": "app-password"}
        assert "app-password" not in credentials.values()
    elif auth_case.network == "telegram":
        assert request.url.path == "/bottoken/getMe"
    if auth_case.network in ("threads", "instagram"):
        expiry = datetime.fromisoformat(credentials["token_expires_at"])
        assert timedelta(days=59) < expiry - datetime.now(timezone.utc) <= timedelta(days=60)


async def test_reauthentication_updates_in_place_without_touching_other_accounts_or_history(
    engine, auth_case
):
    account = create_account(engine, auth_case.network, "personal", auth_case.remote_id)
    other_label = create_account(engine, auth_case.network, "work", "other-remote")
    other_network = create_account(
        engine, "telegram" if auth_case.network != "telegram" else "twitter", "personal", "destination"
    )
    set_credentials(engine, account.id, {key: "old-value" for key in auth_case.credentials})
    set_credentials(engine, other_label.id, {"access_token": "other-label-token"})
    set_credentials(engine, other_network.id, {"access_token": "other-network-token"})
    mark_synced(engine, account.id, "source-post", other_network.id, "destination-post")

    result = await auth_case.module.authenticate(engine, label="personal")

    assert result.id == account.id
    assert len(list_accounts(engine)) == 3
    assert get_all_credentials(engine, account.id).items() >= auth_case.credentials.items()
    assert get_all_credentials(engine, other_label.id) == {"access_token": "other-label-token"}
    assert get_all_credentials(engine, other_network.id) == {"access_token": "other-network-token"}
    assert get_dest_post_id(engine, account.id, "source-post", other_network.id) == "destination-post"


async def test_new_label_creates_independent_account(engine, auth_case):
    existing = create_account(engine, auth_case.network, "work", "existing-remote")
    set_credentials(engine, existing.id, {"token": "keep-me"})

    new = await auth_case.module.authenticate(engine, label="personal")

    assert new.id != existing.id
    assert len(list_accounts(engine, auth_case.network)) == 2
    assert get_all_credentials(engine, existing.id) == {"token": "keep-me"}


@pytest.mark.parametrize("existing", [False, True], ids=["new-account", "reauthentication"])
async def test_rejected_authentication_does_not_create_or_overwrite_state(
    engine, auth_case, existing
):
    if existing:
        account = create_account(engine, auth_case.network, "personal", "original-remote")
        set_credentials(engine, account.id, {"token": "original-token"})
    accounts_before = list_accounts(engine)
    auth_case.route.respond(401, json={"error": {"message": "Rejected"}})

    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        await auth_case.module.authenticate(engine, label="personal")

    assert list_accounts(engine) == accounts_before
    if existing:
        assert get_all_credentials(engine, account.id) == {"token": "original-token"}


async def test_empty_required_input_is_rejected_before_persisting(engine, auth_case, monkeypatch, request):
    values = iter(["", *auth_case.inputs[1:]])
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))

    rejected = False
    try:
        await auth_case.module.authenticate(engine, label="personal")
    except (ValueError, RuntimeError):
        rejected = True

    assert rejected
    assert list_accounts(engine) == []
    assert not auth_case.route.called


async def test_reauthentication_keeps_remote_identity_consistent(engine, auth_case, request):
    account = create_account(engine, auth_case.network, "personal", "old-remote")

    await auth_case.module.authenticate(engine, label="personal")

    assert get_account(engine, account.id).remote_id == auth_case.remote_id


@pytest.mark.parametrize("network", ["threads", "instagram"])
async def test_valid_meta_auth_survives_token_upgrade_failure(
    engine, monkeypatch, http_mock, network
):
    config = THREADS_TOKEN if network == "threads" else INSTAGRAM_TOKEN
    values = iter(["valid-token", "secret", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))
    http_mock.get(config.exchange.url).respond(400, json={"error": {"message": "Not eligible"}})
    http_mock.get(config.refresh.url).respond(400, json={"error": {"message": "Too young"}})
    url = "https://graph.threads.net/v1.0/me" if network == "threads" else "https://graph.instagram.com/v21.0/me"
    http_mock.get(url).respond(200, json={"id": "remote-1", "username": "alice"})

    account = await import_module(f"apis.{network}").authenticate(engine)

    credentials = get_all_credentials(engine, account.id)
    assert credentials["access_token"] == "valid-token"
    assert credentials["token_expires_at"] == ""
    assert "token_renewed_at" not in credentials


async def test_instagram_facebook_login_stores_correct_grant_credentials(
    engine, monkeypatch, http_mock
):
    from apis.instagram import authenticate

    values = iter(["facebook-token", "app-id", "secret", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))
    http_mock.get("https://graph.instagram.com/v21.0/me").respond(401, json={"error": {"message": "Wrong login type"}})
    http_mock.get("https://graph.facebook.com/v21.0/me/accounts").respond(
        200, json={"data": [{"id": "page-without-instagram"}, {"instagram_business_account": {"id": "ig-1", "username": "alice"}}]}
    )
    grant = http_mock.get(FACEBOOK_TOKEN.exchange.url).respond(
        200, json={"access_token": "long-lived-facebook", "expires_in": 60 * 86400}
    )

    account = await authenticate(engine)

    credentials = get_all_credentials(engine, account.id)
    assert credentials.items() >= {
        "token_kind": "facebook", "app_id": "app-id", "app_secret": "secret",
        "access_token": "long-lived-facebook", "user_id": "ig-1",
    }.items()
    params = grant.calls[0].request.url.params
    assert params["client_id"] == "app-id"
    assert params["client_secret"] == "secret"
    assert params["fb_exchange_token"] == "facebook-token"
    assert get_account(engine, account.id).remote_id == "ig-1"


@pytest.mark.parametrize("refresh_succeeds", [True, False])
async def test_bluesky_expired_session_refreshes_once_and_persists_only_valid_tokens(
    engine, http_mock, refresh_succeeds
):
    from apis.bluesky import fetch_posts

    account = create_account(engine, "bluesky", "main", "did:plc:alice")
    credentials = {
        "handle": "alice.test", "did": "did:plc:alice", "pds_url": "https://pds.test",
        "access_jwt": "expired", "refresh_jwt": "old-refresh",
    }
    set_credentials(engine, account.id, credentials)
    timeline = http_mock.get("https://pds.test/xrpc/app.bsky.feed.getAuthorFeed")
    timeline.side_effect = [
        httpx.Response(401, json={"error": "ExpiredToken"}),
        httpx.Response(200, json={"feed": []}),
    ]
    refresh = http_mock.post("https://pds.test/xrpc/com.atproto.server.refreshSession").respond(
        200 if refresh_succeeds else 401,
        json={"accessJwt": "new-access", "refreshJwt": "new-refresh"} if refresh_succeeds else {"error": "InvalidToken"},
    )

    if refresh_succeeds:
        assert await fetch_posts(engine, account.id) == []
        assert timeline.call_count == 2
        assert timeline.calls[-1].request.headers["Authorization"] == "Bearer new-access"
        stored = get_all_credentials(engine, account.id)
        assert stored == {**credentials, "access_jwt": "new-access", "refresh_jwt": "new-refresh"}
    else:
        with pytest.raises(RuntimeError):
            await fetch_posts(engine, account.id)
        assert timeline.call_count == 1
        assert get_all_credentials(engine, account.id) == credentials
    assert refresh.call_count == 1
    assert refresh.calls[0].request.headers["Authorization"] == "Bearer old-refresh"


async def test_authentication_updates_only_selected_project(engine, auth_case):
    from db.projects import create_project
    project = create_project(engine, "Work")
    default = create_account(engine, auth_case.network, "personal", "original")
    selected = create_account(engine, auth_case.network, "personal", "previous", project_id=project.id)
    set_credentials(engine, default.id, {"untouched": "default-secret"})

    result = await auth_case.module.authenticate(engine, label="personal", project_id=project.id)

    assert result.id == selected.id
    assert result.project_id == project.id
    assert get_account(engine, selected.id).remote_id == auth_case.remote_id
    assert get_all_credentials(engine, selected.id).items() >= auth_case.credentials.items()
    assert get_account(engine, default.id).remote_id == "original"
    assert get_all_credentials(engine, default.id) == {"untouched": "default-secret"}


async def test_adapter_authentication_persists_requested_role(engine, auth_case):
    account = await auth_case.module.authenticate(engine, label="personal", sync_role="source_only")
    assert get_account(engine, account.id).sync_role == "source_only"
