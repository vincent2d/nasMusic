import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import netease_mobile
from http_server import BoundedThreadingHTTPServer
from server import Handler, MobileProxyHandler
from state_store import state_db_connect


class NeteaseMobileTests(unittest.TestCase):
    origin = "https://music.example.test"
    mobile = "/api/netease/mobile"
    favorite = "/api/netease/favorite"
    favorite_payload = {"track_id": "netease:123", "liked": True}
    forwarded = {"X-Forwarded-Host": "music.example.test", "X-Forwarded-Proto": "https"}
    gateway = {"X-Fnaudio-Internal": "1"}

    def setUp(self):
        cache = self.enterContext(tempfile.TemporaryDirectory())
        self.config = {"cache_dir": cache, "structured_log_path": str(Path(cache) / "fnaudio.jsonl")}
        self.direct = self.start_server(Handler)
        self.phone = self.start_server(MobileProxyHandler)

    def start_server(self, handler):
        server = BoundedThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.config = self.config
        server.mobile_origin = self.origin
        server.web_root = ""
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def request(self, port, path, payload=None, *, headers=None, content_type="application/json"):
        request_headers = dict(self.forwarded if port == self.phone else {})
        if payload is not None:
            request_headers.update({"Content-Type": content_type, "Origin": self.origin})
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET" if payload is None else "POST", path,
                         None if payload is None else json.dumps(payload), request_headers)
            response = conn.getresponse()
            body = response.read()
            data = json.loads(body) if response.getheader("Content-Type", "").startswith("application/json") else body
            return response.status, data, response.getheader("Set-Cookie")
        finally:
            conn.close()

    @patch("server.NETEASE.set_liked")
    def test_phone_session_needs_gateway_approval_and_survives_restart_until_revoked(self, provider):
        for headers in ({}, self.gateway, {"X-Fnaudio-Scopes": "settings.write", "X-Fnaudio-Trim-Token": "x"}):
            self.assertEqual(self.request(self.phone, self.favorite, self.favorite_payload, headers=headers)[0], 403)
        self.assertEqual(self.request(self.phone, "/api/playback/targets")[0], 200)
        self.assertEqual(self.request(self.phone, "/api/config/library", {}, headers=self.gateway)[0], 403)
        self.assertEqual(self.request(self.phone, "/internal/netease/123")[0], 403)
        self.assertEqual(self.request(self.direct, self.favorite, self.favorite_payload, headers=self.forwarded)[0], 403)
        self.assertEqual(self.request(self.phone, self.mobile, {"action": "request"}, headers={"Origin": "https://other.test"})[0], 403)

        status, data, cookie = self.request(self.phone, self.mobile, {"action": "request"})
        self.assertEqual(status, 200)
        self.assertFalse(data["authorized"])
        self.assertIn("; Secure; HttpOnly; SameSite=Strict", cookie)
        self.assertTrue(cookie.startswith(netease_mobile.COOKIE_NAME + "="))
        phone_cookie = {"Cookie": cookie.split(";", 1)[0]}
        approval = {"action": "approve", "code": data["code"]}
        self.assertEqual(self.request(self.phone, self.favorite, self.favorite_payload, headers=phone_cookie)[0], 403)
        self.assertEqual(self.request(self.phone, self.mobile, approval, headers=self.gateway)[0], 403)
        self.assertEqual(self.request(self.direct, self.mobile, approval, headers=self.gateway, content_type="text/plain")[0], 403)
        self.assertEqual(self.request(self.direct, self.mobile, approval)[0], 403)
        provider.assert_not_called()
        self.assertEqual(self.request(self.direct, self.mobile, approval, headers=self.gateway)[0], 200)
        self.assertEqual(self.request(self.direct, self.mobile, approval, headers=self.gateway)[0], 400)

        self.phone = self.start_server(MobileProxyHandler)
        self.assertEqual(self.request(self.phone, self.favorite, self.favorite_payload, headers={**phone_cookie, "Origin": "https://other.test"})[0], 403)
        self.assertEqual(self.request(self.phone, self.favorite, self.favorite_payload)[0], 403)
        self.assertEqual(self.request(self.direct, self.favorite, self.favorite_payload, headers=phone_cookie)[0], 403)
        status, data, _ = self.request(self.phone, self.favorite, self.favorite_payload, headers=phone_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["state"]["favorites"], ["netease:123"])
        provider.assert_called_once_with("123", True)

        self.assertEqual(self.request(self.direct, self.mobile, {"action": "revoke"}, headers=self.gateway)[0], 200)
        self.assertEqual(self.request(self.phone, self.favorite, self.favorite_payload, headers=phone_cookie)[0], 403)
        self.assertEqual(provider.call_count, 1)

    def test_expired_pending_approval_cannot_be_approved(self):
        _, data, _ = self.request(self.phone, self.mobile, {"action": "request"})
        with state_db_connect(self.config) as conn:
            conn.execute("UPDATE netease_mobile_devices SET expires = 0")
        self.assertEqual(self.request(self.direct, self.mobile, {"action": "approve", "code": data["code"]}, headers=self.gateway)[0], 400)
