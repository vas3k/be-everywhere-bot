"""Shared text normalization helpers for social network API clients."""

import re
from collections.abc import Sequence


def strip_trailing_patterns(
    text: str,
    patterns: Sequence[re.Pattern[str]],
) -> str:
    text = text.rstrip()
    for pattern in patterns:
        text = pattern.sub("", text).rstrip()
    return text
