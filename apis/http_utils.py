"""Shared HTTP helpers for social network API clients."""

from collections.abc import Callable
from typing import Any

import httpx


def parse_error_detail(response: httpx.Response) -> Any:
    detail: Any = response.text
    try:
        detail = response.json()
    except Exception:
        pass
    return detail


def format_api_error(
    api_name: str,
    status: int,
    detail: Any,
    *,
    extra: Callable[[int, Any], str | None] | None = None,
) -> str:
    if extra is not None:
        message = extra(status, detail)
        if message is not None:
            return message
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            msg = err.get("message", err)
            return f"{api_name} API request failed ({status}): {msg}"
        msg = detail.get("message") or err or detail
        return f"{api_name} API request failed ({status}): {msg}"
    return f"{api_name} API request failed ({status}): {detail}"


def twitter_api_error_extra(status: int, detail: Any) -> str | None:
    if status == 402 and isinstance(detail, dict):
        title = detail.get("title", "")
        if title == "CreditsDepleted" or "credits" in str(detail.get("type", "")).lower():
            return (
                "X API credits depleted (HTTP 402).\n"
                "Top up at https://developer.x.com/en/portal/dashboard"
            )
    return None
