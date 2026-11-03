"""All bot text lives in en.json. Placeholders are filled with str.format_map."""
import html
import json
import os

from OpusApi import config

_PATH = config.STRINGS_FILE or os.path.join(os.path.dirname(os.path.abspath(__file__)), "en.json")
with open(_PATH, encoding="utf-8") as _f:
    _DATA = json.load(_f)


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def T(key: str, **kw) -> str:
    return _DATA["texts"][key].format_map(_Safe(kw))


def B(key: str, **kw) -> str:
    return _DATA["buttons"][key].format_map(_Safe(kw))


def esc(value) -> str:
    """Escape user-provided text before it goes into an HTML message."""
    return html.escape(str(value), quote=False)
