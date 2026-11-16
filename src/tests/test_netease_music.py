from email.message import Message
import io
import unittest
from unittest import mock

import netease_music
from playback.core import PlaybackCore
from server import Handler, playback_queue


class NeteaseMusicTests(unittest.TestCase):
    def test_online_queue_requires_a_nas_target_and_accepts_legacy_track_keys(self):
        core = PlaybackCore()
        with mock.patch("server.PLAYBACK_CORE", core):
            response, status = playback_queue({}, {"queue": [{"track_id": "netease:123"}]})
        self.assertEqual(status, 409)
        self.assertFalse(response["ok"])
        self.assertEqual(core.snapshot()["queue"], [])

    def test_catalog_normalizes_and_remembers_a_playback_row(self):
        music = netease_music.MusicBox()
        with mock.patch("netease_music._request", return_value=[{
            "song_id": 123, "name": "Song", "artist": "Artist", "duration_ms": 45600,
            "url": "https://example.com/untrusted", "path": "/untrusted",
        }]) as source:
            row, = music.search(" Song ")
        source.assert_called_once_with("/search", keyword="Song", type="song", limit=50)
        self.assertEqual(row["id"], "netease:123")
        self.assertEqual(row["duration"], 45.6)
        self.assertNotIn("url", row)
        self.assertNotIn("path", row)
        self.assertEqual(music.track(row["id"]), row)

    def test_online_stream_uses_a_stable_internal_url(self):
        self.assertEqual(netease_music.stream_uri("netease:123", 55555),
                         "http://127.0.0.1:55555/internal/netease/123")
        for invalid in ("local-id", "netease:0", "netease:1?url=x", "netease:1/2"):
            self.assertEqual(netease_music.stream_uri(invalid, 55555), "")

    def test_provider_stream_rejects_previews_and_unexpected_hosts(self):
        music = netease_music.MusicBox()
        url = "https://m701.music.126.net/song.flac"
        with mock.patch("netease_music._request", return_value={"url": url}):
            self.assertEqual(music.stream("123"), url)
        for row in ({"url": url, "freeTrialInfo": {"start": 0, "end": 30}},
                    {"url": "https://m701.music.126.net.example.com/song.flac"},
                    {"url": url + "\r\nX-Extra: 1"}):
            with mock.patch("netease_music._request", return_value=row):
                with self.assertRaises(netease_music.MusicBoxError):
                    music.stream("123")

    def test_stream_stays_local_to_mpd(self):
        handler = mock.Mock()
        with mock.patch("server.NETEASE.open_stream") as stream:
            for host, headers in (("192.168.1.2", {}), ("127.0.0.1", {"X-Fnaudio-Internal": "1"})):
                handler.client_address = (host, 2399)
                handler.headers = headers
                Handler.serve_netease_stream(handler, "123")
                self.assertEqual(handler.send_json.call_args.args[1], 403)
            stream.assert_not_called()

    def test_verified_provider_range_is_advertised_to_mpd(self):
        for requested, offset, response_code in (("", 0, 200), ("bytes=2-", 2, 206)):
            handler = mock.Mock(client_address=("127.0.0.1", 2399), headers={"Range": requested})
            source = io.BytesIO(b"abcd"[offset:])
            source.status = 206
            source.headers = Message()
            source.headers["Content-Type"] = "audio/mpeg"
            source.headers["Content-Length"] = str(4 - offset)
            source.headers["Content-Range"] = f"bytes {offset}-3/4"
            with mock.patch("server.NETEASE.open_stream", return_value=source) as stream:
                Handler.serve_netease_stream(handler, "123")
            stream.assert_called_once_with("123", requested or "bytes=0-")
            handler.send_response.assert_called_once_with(response_code)
            handler.send_header.assert_any_call("Accept-Ranges", "bytes")
            handler.wfile.write.assert_called_once_with(b"abcd"[offset:])
