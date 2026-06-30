import httpx

from utils.http_utils import (
    format_api_error,
    parse_error_detail,
    twitter_api_error_extra,
)


def test_format_api_error_nested_error_dict():
    msg = format_api_error(
        "Threads",
        400,
        {"error": {"message": "Invalid parameter", "code": 100}},
    )
    assert msg == "Threads API request failed (400): Invalid parameter"


def test_format_api_error_message_field():
    msg = format_api_error("Bluesky", 401, {"message": "Token has expired"})
    assert msg == "Bluesky API request failed (401): Token has expired"


def test_format_api_error_plain_text():
    msg = format_api_error("RSS", 500, "internal error")
    assert msg == "RSS API request failed (500): internal error"


def test_parse_error_detail_json():
    response = httpx.Response(400, json={"message": "bad request"})
    assert parse_error_detail(response) == {"message": "bad request"}


def test_parse_error_detail_non_json():
    response = httpx.Response(500, text="server error")
    assert parse_error_detail(response) == "server error"


def test_twitter_api_error_extra_credits_depleted():
    msg = format_api_error(
        "X",
        402,
        {"title": "CreditsDepleted", "type": "about:credits"},
        extra=twitter_api_error_extra,
    )
    assert "credits depleted" in msg.lower()
    assert "developer.x.com" in msg


def test_format_api_error_uses_extra_hook():
    def extra(_status: int, _detail: object) -> str | None:
        return "custom"

    assert format_api_error("X", 500, "x", extra=extra) == "custom"
