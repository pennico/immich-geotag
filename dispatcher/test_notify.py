"""notify(): header values must survive latin-1 (HTTP header encoding).

Regression (2026-10-07): a dispatcher title with an em-dash crashed the
send with "'latin-1' codec can't encode character '\\u2014'" and the
notification was silently lost.
"""
import contextlib
import importlib
import urllib.request

import notify


def _reload_with_topic(monkeypatch, topic="t"):
    monkeypatch.setenv("NTFY_TOPIC", topic)
    return importlib.reload(notify)


def test_header_transliterates_common_punctuation():
    assert notify._header("a — b – c ‘d’ “e” … → f") == \
        "a -- b - c 'd' \"e\" ... -> f"


def test_header_replaces_unmappable_chars():
    # U+1F525 (fire emoji) has no latin-1 form: replaced, not raised.
    assert notify._header("\U0001f525x") == "?x"


def test_notify_sends_unicode_title(monkeypatch):
    mod = _reload_with_topic(monkeypatch)
    captured = {}

    class Resp:
        status = 200

    @contextlib.contextmanager
    def fake_urlopen(req, timeout=None):
        captured["headers"] = req.headers
        yield Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert mod.notify("PR #28 failed — pausing", "body",
                      tags="warning") is True
    # urllib title-cases header names; value must be the transliteration.
    assert captured["headers"]["X-title"] == "PR #28 failed -- pausing"
    assert captured["headers"]["X-tags"] == "warning"


def test_notify_without_topic_is_silent_noop(monkeypatch):
    mod = _reload_with_topic(monkeypatch, topic="")
    assert mod.notify("t", "b") is False
