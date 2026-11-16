import time
import httpx
import pytest
import respx
from apis import twitter
from apis.types import MediaItem
from db.accounts import create_account, set_credentials, get_all_credentials


async def test_refresh_rotates_and_persists_tokens(engine):
    account = create_account(engine, "twitter", "club", "42")
    set_credentials(engine, account.id, {"bearer_token": "old", "user_id": "42", "username": "club", "refresh_token": "refresh", "client_id": "client", "client_secret": "secret", "expires_at": "0"})
    with respx.mock as mock:
        route = mock.post("https://api.x.com/2/oauth2/token").respond(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 7200})
        result = await twitter.ensure_session(engine, account.id)
        assert result["bearer_token"] == "new"
        assert get_all_credentials(engine, account.id)["refresh_token"] == "rotated"
        assert float(result["expires_at"]) > time.time() + 7000
        await twitter.ensure_session(engine, account.id)
        assert route.call_count == 1


async def test_refresh_failure_preserves_credentials(engine):
    account = create_account(engine, "twitter", "club", "42")
    before = {"bearer_token": "old", "user_id": "42", "username": "club", "refresh_token": "refresh", "client_id": "client", "expires_at": "0"}
    set_credentials(engine, account.id, before)
    with respx.mock as mock:
        mock.post("https://api.x.com/2/oauth2/token").respond(429)
        with pytest.raises(RuntimeError, match="HTTP 429"):
            await twitter.ensure_session(engine, account.id)
    assert get_all_credentials(engine, account.id) == before


async def test_photo_upload_uses_v2_and_png_content_type():
    with respx.mock as mock:
        route = mock.post("https://api.x.com/2/media/upload").respond(200, json={"data": {"id": "123"}})
        async with httpx.AsyncClient() as client:
            result = await twitter._upload_media(client, "token", b"\x89PNG\r\n\x1a\nimage", MediaItem(url="https://scr.test/no-extension", media_type="photo"))
        assert result == "123"
        assert b"image/png" in route.calls[0].request.content
