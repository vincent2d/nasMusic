from contextlib import contextmanager, redirect_stdout
import io
import json
import sys
import types
import unittest
from unittest import mock

from netease_write import WRITE_SCRIPT


class NeteaseWriteTests(unittest.TestCase):
    def run_write(self, operation, args, *, code=200, details=None, likes=None, uid=9):
        api = mock.Mock()
        active = False

        @contextmanager
        def cache_disabled():
            nonlocal active
            active = True
            try:
                yield
            finally:
                active = False

        api.session.cache_disabled.side_effect = cache_disabled
        api.get_account_info.return_value = {"account": {"id": uid}}
        mutation = {"code": code}
        if operation == "create":
            mutation.update(id=77, playlist={"trackCount": 999, "name": "stale"})
        responses = iter(details or [])

        def eapi(path, params):
            self.assertTrue(active, "a write or readback used requests-cache")
            if path == "/api/v6/playlist/detail":
                return next(responses)
            return mutation

        api.eapi_request.side_effect = eapi

        def request(method, path, params):
            self.assertTrue(active)
            self.assertEqual((method, path, params), ("POST", "/weapi/song/like/get", {"uid": uid}))
            return {"code": 200, "ids": likes}

        api.request.side_effect = request
        module = types.ModuleType("NEMbox.api")
        module.NetEase = mock.Mock(return_value=api)
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"NEMbox": types.ModuleType("NEMbox"), "NEMbox.api": module}), \
                mock.patch.object(sys, "argv", ["-c", operation, *args]), \
                mock.patch("time.sleep"), redirect_stdout(output):
            exec(WRITE_SCRIPT, {})
        self.assertFalse(active)
        return json.loads(output.getvalue()), api

    @staticmethod
    def detail(ids, **overrides):
        pl = {"id": 77, "name": "测试歌单", "trackCount": len(ids),
              "trackIds": [{"id": sid} for sid in ids], "privacy": 10}
        pl.update(overrides)
        return {"code": 200, "playlist": pl}

    def test_add_502_with_missing_song_is_failure(self):
        result, api = self.run_write("tracks", ["add", "77", "123"], code=502,
                                     details=[self.detail([])] * 3)
        self.assertFalse(result["ok"])
        self.assertFalse(result["receipt"]["verified"])
        self.assertEqual(result["receipt"]["code"], 502)
        self.assertEqual(api.eapi_request.call_count, 4)

    def test_duplicate_add_requires_membership_and_returns_actual_count(self):
        result, _ = self.run_write("tracks", ["add", "77", "123"], code=502,
                                   details=[self.detail([123, 456])])
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["already_exists"])
        self.assertEqual(result["count"], 2)

    def test_200_without_membership_is_failure(self):
        result, _ = self.run_write("tracks", ["add", "77", "123"], details=[self.detail([])] * 3)
        self.assertFalse(result["ok"])

    def test_delayed_readback_retries_reads_without_repeating_write(self):
        result, api = self.run_write("tracks", ["add", "77", "123"],
                                     details=[self.detail([]), self.detail([123])])
        self.assertTrue(result["verified"])
        writes = [call for call in api.eapi_request.call_args_list
                  if call.args[0] == "/api/playlist/manipulate/tracks"]
        self.assertEqual(len(writes), 1)

    def test_delete_requires_absence_and_does_not_accept_502(self):
        for code, ids, expected in ((200, [], True), (200, [123], False), (502, [], False)):
            with self.subTest(code=code, ids=ids):
                result, _ = self.run_write("tracks", ["del", "77", "123"], code=code,
                                           details=[self.detail(ids)] * 3)
                self.assertEqual(result["ok"], expected)

    def test_incomplete_or_invalid_playlist_readback_cannot_confirm_removal(self):
        for detail in (self.detail([], trackCount=1), self.detail([], id=88),
                       self.detail([], trackIds=None), {"code": 403},
                       self.detail([], trackIds=[{"id": "123"}], trackCount=1)):
            with self.subTest(detail=detail):
                result, _ = self.run_write("tracks", ["del", "77", "123"], details=[detail])
                self.assertFalse(result["ok"])

    def test_like_and_unlike_require_fresh_membership(self):
        for desired, ids, expected in (("true", [123], True), ("true", [], False),
                                       ("false", [], True), ("false", [123], False),
                                       ("false", None, False)):
            with self.subTest(desired=desired, ids=ids):
                result, api = self.run_write("like", ["123", desired], likes=ids)
                self.assertEqual(result["ok"], expected)
                self.assertEqual(api.eapi_request.call_count, 1)

    def test_rejected_write_is_not_hidden_by_a_matching_read(self):
        result, api = self.run_write("like", ["123", "true"], code=403, likes=[123])
        self.assertFalse(result["ok"])
        api.request.assert_not_called()

    def test_create_uses_readback_metadata_and_never_repeats_creation(self):
        result, api = self.run_write("create", ["测试歌单", "1"], details=[self.detail([])])
        self.assertTrue(result["verified"])
        self.assertEqual(result["track_count"], 0)
        self.assertEqual(result["name"], "测试歌单")
        self.assertTrue(result["private"])
        self.assertEqual(api.eapi_request.call_count, 2)
        result, api = self.run_write("create", ["测试歌单", "1"], details=[{"code": 404}])
        self.assertFalse(result["ok"])
        self.assertEqual(api.eapi_request.call_count, 2)

    def test_expired_login_does_not_send_a_write(self):
        result, api = self.run_write("like", ["123", "true"], uid=None)
        self.assertEqual(result["error"], "not_logged_in")
        api.eapi_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
