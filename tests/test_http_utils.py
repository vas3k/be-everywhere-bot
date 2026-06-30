from apis.http_utils import format_api_error


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
