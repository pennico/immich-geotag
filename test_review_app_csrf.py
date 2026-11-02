"""Cross-site request guards of the time-fill review app's POST /apply.

Runs the real HTTP handler on an ephemeral loopback port; the write path
(time_fill.apply_clusters) is mocked, so nothing touches Immich.
"""

import argparse
import email.message
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import time_fill_review_app as app

TOKEN = "test-token-123"


def headers(**kw):
    msg = email.message.Message()
    for k, v in kw.items():
        msg[k.replace("_", "-")] = v
    return msg


class TestCheckApplyRequest(unittest.TestCase):
    PORT = 8788

    def ok_headers(self, **over):
        h = {"Host": "127.0.0.1:8788", "Origin": "http://127.0.0.1:8788",
             "Content_Type": "application/json", "X_Apply_Token": TOKEN}
        h.update(over)
        return headers(**{k: v for k, v in h.items() if v is not None})

    def check(self, **over):
        return app.check_apply_request(self.ok_headers(**over), self.PORT, TOKEN)

    def test_same_origin_with_token_allowed(self):
        self.assertIsNone(self.check())

    def test_localhost_names_allowed(self):
        self.assertIsNone(self.check(Host="localhost:8788",
                                     Origin="http://localhost:8788"))

    def test_missing_origin_allowed_with_token(self):
        # non-browser clients (curl) send no Origin; the token still applies
        self.assertIsNone(self.check(Origin=None))

    def test_foreign_origin_refused(self):
        self.assertEqual(self.check(Origin="https://evil.example")[0], 403)

    def test_null_origin_refused(self):
        self.assertEqual(self.check(Origin="null")[0], 403)

    def test_other_port_origin_refused(self):
        self.assertEqual(self.check(Origin="http://127.0.0.1:9999")[0], 403)

    def test_rebound_host_refused(self):
        # DNS rebinding: attacker.example resolves to 127.0.0.1
        self.assertEqual(self.check(Host="attacker.example:8788")[0], 403)

    def test_missing_host_refused(self):
        self.assertEqual(self.check(Host=None)[0], 403)

    def test_text_plain_refused(self):
        # the no-preflight "simple request" a foreign page can send
        self.assertEqual(self.check(Content_Type="text/plain")[0], 415)

    def test_json_with_charset_allowed(self):
        self.assertIsNone(self.check(Content_Type="application/json; charset=utf-8"))

    def test_missing_token_refused(self):
        self.assertEqual(self.check(X_Apply_Token=None)[0], 403)

    def test_wrong_token_refused(self):
        self.assertEqual(self.check(X_Apply_Token="guess")[0], 403)

    def test_empty_server_token_refuses_everything(self):
        h = self.ok_headers(X_Apply_Token="")
        self.assertEqual(app.check_apply_request(h, self.PORT, "")[0], 403)


class TestInjectToken(unittest.TestCase):
    def test_meta_inserted_after_head(self):
        out = app.inject_token(b"<html><head><title>x</title></head></html>", "abc")
        self.assertIn(b'<head><meta name="apply-token" content="abc">', out)

    def test_rendered_page_sends_token_header(self):
        page = app.render_page([], [], source="time", port=8788)
        self.assertIn('"X-Apply-Token": APPLY_TOKEN', page)
        self.assertIn('meta[name="apply-token"]', page)
        self.assertIn("<head>", page)


class TestServerEndToEnd(unittest.TestCase):
    """Real handler on 127.0.0.1:<ephemeral>; apply_clusters mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "index.html"), "w") as f:
            f.write("<!DOCTYPE html><html><head><title>t</title></head></html>")
        args = argparse.Namespace(out=self.tmp.name, port=0, source="time",
                                  clusters=os.path.join(self.tmp.name, "c.csv"))
        self.server = app.make_server(args, TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        patcher = mock.patch.object(app.time_fill, "apply_clusters",
                                    return_value=(3, {"C001": 3}))
        self.apply = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, hdrs=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=hdrs or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def post(self, **over):
        h = {"Content-Type": "application/json",
             "Origin": f"http://127.0.0.1:{self.port}",
             "X-Apply-Token": TOKEN}
        h.update(over)
        h = {k: v for k, v in h.items() if v is not None}
        return self.request("POST", "/apply",
                            json.dumps({"clusters": ["C001"]}), h)

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_index_carries_token(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(f'content="{TOKEN}"'.encode(), body)

    def test_valid_apply_reaches_write_path(self):
        status, body = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["applied"], 3)
        self.apply.assert_called_once()

    def test_cross_site_simple_post_refused(self):
        # what fetch(..., {mode: "no-cors"}) from a foreign page looks like
        status, _ = self.post(**{"Content-Type": "text/plain",
                                 "Origin": "https://evil.example",
                                 "X-Apply-Token": None})
        self.assertEqual(status, 403)
        self.apply.assert_not_called()

    def test_tokenless_post_refused(self):
        status, _ = self.post(**{"X-Apply-Token": None})
        self.assertEqual(status, 403)
        self.apply.assert_not_called()

    def test_text_plain_with_token_refused(self):
        status, _ = self.post(**{"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
