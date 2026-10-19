"""Tiny fa/en helper for server-side messages.

The browser sends its UI language in the X-Lang header; background work (sync loop,
certificate job) falls back to the admin's last chosen language stored in settings.
"""
import contextvars

LANGS = ("fa", "en")
_req_lang = contextvars.ContextVar("mtp_lang", default=None)


def set_request_lang(value):
    _req_lang.set(value if value in LANGS else None)


def lang():
    value = _req_lang.get()
    if value:
        return value
    try:
        from . import db
        value = db.get_setting("ui_lang", "fa")
    except Exception:
        value = "fa"
    return value if value in LANGS else "fa"


def L(fa, en):
    """Pick the message for the current language."""
    return en if lang() == "en" else fa
