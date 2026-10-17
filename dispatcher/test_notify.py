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


def _fake_urlopen_seq(outcomes, seen):
    """urlopen stand-in: each call pops an outcome (an exception or a status)."""
    class Resp:
        def __init__(self, status):
            self.status = status

    @contextlib.contextmanager
    def fake(req, timeout=None):
        seen.append(req)
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        yield Resp(o)
    return fake


def _http_error(code):
    import urllib.error
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_notify_retries_network_and_5xx_then_succeeds(monkeypatch):
    mod = _reload_with_topic(monkeypatch)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_seq(
        [OSError("connection refused"), _http_error(503), 200], seen))
    assert mod.notify("t", "b") is True
    assert len(seen) == 3


def test_notify_gives_up_after_three_attempts(monkeypatch):
    mod = _reload_with_topic(monkeypatch)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_seq(
        [OSError("x")] * 5, seen))
    assert mod.notify("t", "b") is False
    assert len(seen) == 3


def test_notify_does_not_retry_auth_errors(monkeypatch):
    mod = _reload_with_topic(monkeypatch)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_seq(
        [_http_error(403), 200], seen))
    assert mod.notify("t", "b") is False
    assert len(seen) == 1


def test_notify_url_sets_click_and_action(monkeypatch):
    mod = _reload_with_topic(monkeypatch)
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_seq([200], seen))
    assert mod.notify("t", "b", url="https://github.com/x/1") is True
    assert seen[0].headers["Click"] == "https://github.com/x/1"
    assert "https://github.com/x/1" in seen[0].headers["Actions"]
