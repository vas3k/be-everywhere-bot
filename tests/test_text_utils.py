import re

from apis.text_utils import strip_trailing_patterns


def test_strip_trailing_patterns_strips_sequential_suffixes():
    trailing = re.compile(r"\s+suffix$")
    assert strip_trailing_patterns("hello suffix", [trailing]) == "hello"
    assert strip_trailing_patterns("hello suffix suffix", [trailing]) == "hello suffix"
