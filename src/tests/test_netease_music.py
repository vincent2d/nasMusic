from email.message import Message
import io
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

import netease_music
from playback.core import PlaybackCore
from playback.plugins import DlnaPushOutput
from server import Handler, playback_queue
from state_store import load_user_state, save_netease_favorite, save_user_state


class NeteaseMusicTests(unittest.TestCase):
    def test_favorite_write_requires_the_fn_os_gateway(self):
        path = "/api/netease/favorite"
        for peer, headers in (
            ("192.168.1.2", {}),
            ("192.168.1.2", {"X-Fnaudio-Internal": "1"}),
            ("127.0.0.1", {}),
        ):
            handler = Handler.__new__(Handler)
            handler.headers = headers
            handler.client_address = (peer, 51000)
            handler.send_json = mock.Mock()
            self.assertFalse(handler.authorize_api_request("POST", path))
            self.assertEqual(handler.send_json.call_args.args[1], 403)
        handler.headers = {"X-Fnaudio-Internal": "1"}
        self.assertTrue(handler.authorize_api_request("POST", path))

    def test_favorite_requires_explicit_json_boolean_and_persists_only_after_success(self):
        handler = mock.Mock(headers={"Content-Type": "text/plain"})
        with tempfile.TemporaryDirectory() as cache:
            handler.server.config = {"cache_dir": cache}
            with mock.patch("server.NETEASE.set_liked") as provider:
                Handler.set_netease_favorite(handler, {"track_id": "netease:123", "liked": True})
                self.assertEqual(handler.send_json.call_args.args[1], 415)
                handler.headers = {"Content-Type": "application/json; charset=utf-8"}
                Handler.set_netease_favorite(handler, {"track_id": "netease:123", "liked": "true"})
                self.assertEqual(handler.send_json.call_args.args[1], 400)
                provider.assert_not_called()
                provider.side_effect = netease_music.MusicBoxError("无法同步")
                Handler.set_netease_favorite(handler, {"track_id": "netease:123", "liked": True})
                self.assertEqual(handler.send_json.call_args.args[1], 502)
                self.assertEqual(load_user_state(handler.server.config)["favorites"], [])
                provider.side_effect = None
                for liked, favorites in ((True, ["netease:123"]), (False, [])):
                    Handler.set_netease_favorite(handler, {"track_id": "netease:123", "liked": liked})
                    provider.assert_called_with("123", liked)
                    self.assertEqual(handler.send_json.call_args.args[0]["state"]["favorites"], favorites)

    def test_online_favorite_survives_a_full_or_stale_local_save(self):
        with tempfile.TemporaryDirectory() as cache:
            config = {"cache_dir": cache}
            save_netease_favorite(config, "netease:123", True)
            state = save_user_state(config, {"favorites": ["local", "netease:456"]})
            self.assertCountEqual(state["favorites"], ["local", "netease:123"])
            state = save_user_state(config, {"changes": {
                "favorites_add": ["local-2", "netease:456"],
                "favorites_remove": ["netease:123"],
            }}, expected_revision=0)
            self.assertCountEqual(state["favorites"], ["local", "local-2", "netease:123"])

    def test_provider_uses_explicit_desired_state_and_reports_expired_login(self):
        music = netease_music.MusicBox()
        with mock.patch("netease_music.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok": true, "verified": true}\n', "")
            music.set_liked("123", False)
            self.assertEqual(run.call_args.args[0][-2:], ("123", "false"))
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok": false, "error": "not_logged_in"}\n', "")
            with self.assertRaisesRegex(netease_music.MusicBoxError, "登录网易云"):
                music.set_liked("123", True)

    def test_favorite_does_not_accept_an_unverified_success(self):
        music = netease_music.MusicBox()
        for response in ('{"ok": true}', '{"ok": true, "verified": false}', 'ok', '[]'):
            with self.subTest(response=response), mock.patch("netease_music.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, response, "")
                with self.assertRaises(netease_music.MusicBoxError):
                    music.set_liked("123", True)

    def test_online_queue_requires_a_nas_target_and_accepts_legacy_track_keys(self):
        core = PlaybackCore()
        with mock.patch("server.PLAYBACK_CORE", core):
            response, status = playback_queue({}, {"queue": [{"track_id": "netease:123"}]})
        self.assertEqual(status, 409)
        self.assertFalse(response["ok"])
        self.assertEqual(core.snapshot()["queue"], [])

    def test_online_queue_accepts_dlna_and_uses_provider_metadata(self):
        core = PlaybackCore()
        core.registry.register(DlnaPushOutput())
        core.set_target("speaker", "dlna_push")
        with tempfile.TemporaryDirectory() as cache, mock.patch("server.PLAYBACK_CORE", core), mock.patch(
            "server.NETEASE.track", return_value={"id": "netease:123", "title": "Trusted song", "duration": 120}
        ):
            response, status = playback_queue({"cache_dir": cache}, {
                "queue": [{"track_id": "netease:123", "title": "Spoofed", "uri": "http://untrusted"}],
                "current_id": "netease:123",
            })
        self.assertEqual(status, 200)
        self.assertEqual(core.current_item()["title"], "Trusted song")
        self.assertNotIn("uri", core.current_item())

    def test_upnp_online_resource_routes_to_the_lan_stream(self):
        handler = mock.Mock()
        Handler.serve_upnp_media(handler, "netease%3A123/song.mp3", {}, True)
        handler.serve_netease_stream.assert_called_once_with("123", True, dlna=True)

    def test_dlna_stream_is_lan_only_and_preserves_range(self):
        for peer, mobile in (("8.8.8.8", False), ("192.168.1.2", True)):
            handler = mock.Mock(client_address=(peer, 1234), mobile_proxy=mobile, headers={})
            with mock.patch("server.NETEASE.open_stream") as source:
                Handler.serve_netease_stream(handler, "123", dlna=True)
                source.assert_not_called()
            self.assertEqual(handler.send_json.call_args.args[1], 403)
        handler = mock.Mock(client_address=("192.168.1.2", 1234), mobile_proxy=False,
                            headers={"Range": "bytes=2-3"})
        source = io.BytesIO(b"cd")
        source.status = 206
        source.headers = Message()
        source.headers["Content-Type"] = "audio/mpeg"
        source.headers["Content-Length"] = "2"
        source.headers["Content-Range"] = "bytes 2-3/4"
        with mock.patch("server.NETEASE.open_stream", return_value=source):
            Handler.serve_netease_stream(handler, "123", dlna=True)
        handler.send_response.assert_called_once_with(206)
        handler.wfile.write.assert_called_once_with(b"cd")

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

    def test_search_accepts_musicbox_cli_song_name_and_duration_in_seconds(self):
        music = netease_music.MusicBox()
        with mock.patch("netease_music._request", return_value=[{
            "song_id": 254574, "song_name": "后来", "artist": "刘若英", "duration": 341,
        }]):
            row, = music.search("后来 刘若英")
        self.assertEqual(row["title"], "后来")
        self.assertEqual(row["duration"], 341)

    def test_search_rows_without_artwork_are_hydrated_from_song_details(self):
        music = netease_music.MusicBox()
        search_row = {"song_id": 254574, "song_name": "后来", "artist": "刘若英", "duration": 341}
        cover = "https://p1.music.126.net/album.jpg"
        with mock.patch("netease_music._request", return_value=[search_row]):
            self.assertEqual(music.search("后来")[0]["cover_url"], "")
        with mock.patch("netease_music._request", return_value=[{**search_row, "album_pic_url": cover}]) as detail:
            for _ in range(2):
                self.assertEqual(music.hydrate(["netease:254574"])[0]["cover_url"], cover)
            detail.assert_called_once_with("/songs/detail", ids="254574")
        with mock.patch("netease_music._request", return_value=[search_row]):
            self.assertEqual(music.search("后来")[0]["cover_url"], cover)

    def test_missing_artwork_detail_failure_keeps_search_metadata_retryable(self):
        music = netease_music.MusicBox()
        music._remember([{"song_id": 123, "name": "Song"}])
        with mock.patch("netease_music._request", side_effect=netease_music.MusicBoxError("暂时不可用")):
            row, = music.hydrate(["netease:123"])
            self.assertEqual(row["title"], "Song")
            self.assertEqual(row["cover_url"], "")
        with mock.patch("netease_music._request", return_value=[{"song_id": 123, "name": "Song", "album_pic_url": "https://p1.music.126.net/cover.jpg"}]):
            self.assertTrue(music.hydrate(["netease:123"])[0]["cover_url"])

    def test_similar_songs_follow_netease_order_and_use_musicbox_metadata(self):
        music = netease_music.MusicBox()
        recommendations = {"code": 200, "songs": [
            {"id": 456}, {"id": 123}, {"id": 789}, {"id": 456}, {"id": "bad"},
        ]}
        with mock.patch("netease_music._read_json", return_value=recommendations) as source, \
                mock.patch("netease_music._request", return_value=[
                    {"song_id": 789, "name": "Second"},
                    {"song_id": 456, "name": "First", "duration_ms": 11000},
                ]) as detail:
            rows = music.similar("123")
        self.assertIn("songid=123", source.call_args.args[0].full_url)
        detail.assert_called_once_with("/songs/detail", ids="456,789")
        self.assertEqual([row["id"] for row in rows], ["netease:456", "netease:789"])
        self.assertEqual(music.track("netease:456")["duration"], 11)

    def test_similar_songs_distinguish_empty_results_from_provider_errors(self):
        music = netease_music.MusicBox()
        with mock.patch("netease_music._request") as detail:
            with mock.patch("netease_music._read_json", return_value={"code": 200, "songs": []}):
                self.assertEqual(music.similar("123"), [])
            with mock.patch("netease_music._read_json", return_value={"code": 403}):
                with self.assertRaises(netease_music.MusicBoxError):
                    music.similar("123")
            detail.assert_not_called()

    def test_similar_endpoint_rejects_an_invalid_netease_id(self):
        handler = mock.Mock()
        with mock.patch("server.NETEASE.similar") as similar:
            Handler.serve_netease_catalog(handler, "/api/netease/similar", {"id": ["123/456"]})
            self.assertEqual(handler.send_json.call_args.args[1], 400)
            similar.assert_not_called()

    def test_comments_normalize_hot_and_latest_rows_and_keep_page_boundaries(self):
        music = netease_music.MusicBox()
        payload = {"code": 200, "total": 42, "more": True, "comments": [
            {"content": "第一行\n第二行", "user": {"nickname": " 听众 "}, "likedCount": 3, "time": 1700000000000},
            None, {"content": 123}, {"content": "匿名", "likedCount": -1, "time": "bad"},
        ], "hotComments": [{"content": "热门", "user": None}]}
        with mock.patch("netease_music._read_json", return_value=payload) as source:
            result = music.comments("123")
            self.assertEqual(result["comments"], [
                {"user": "听众", "content": "第一行\n第二行", "likes": 3, "time": 1700000000000},
                {"user": "", "content": "匿名", "likes": 0, "time": 0},
            ])
            self.assertEqual(result["hot_comments"][0]["content"], "热门")
            self.assertEqual(result["total"], 42)
            self.assertTrue(result["more"])
            self.assertEqual(music.comments("123", 20)["hot_comments"], [])
            request = source.call_args.args[0]
            self.assertEqual(request.full_url, "https://music.163.com/api/v1/resource/comments/R_SO_4_123?offset=20&limit=20&total=true")
            self.assertIsNone(request.get_header("Cookie"))

    def test_comments_distinguish_no_comments_from_provider_failure(self):
        music = netease_music.MusicBox()
        with mock.patch("netease_music._read_json", return_value={"code": 200, "comments": []}):
            self.assertEqual(music.comments("123")["comments"], [])
        for payload in ({"code": 403}, {"code": 200, "comments": {}}, []):
            with mock.patch("netease_music._read_json", return_value=payload):
                with self.assertRaises(netease_music.MusicBoxError):
                    music.comments("123")

    def test_comments_endpoint_validates_id_and_offset_and_reports_provider_failure(self):
        handler = mock.Mock()
        handler.path = "/api/netease/comments?id=123&offset=20"
        Handler.handle_get(handler)
        handler.authorize_api_request.assert_called_once_with("GET", "/api/netease/comments")
        with mock.patch("server.NETEASE.comments", return_value={"comments": [], "total": 0}) as provider:
            for query in ({"id": ["bad"]}, {"id": ["123"], "offset": ["-1"]},
                          {"id": ["123"], "offset": ["1000001"]}, {"id": ["123"], "offset": ["1.5"]}):
                Handler.serve_netease_catalog(handler, "/api/netease/comments", query)
                self.assertEqual(handler.send_json.call_args.args[1], 400)
            provider.assert_not_called()
            Handler.serve_netease_catalog(handler, "/api/netease/comments", {"id": ["123"], "offset": ["20"]})
            provider.assert_called_once_with("123", 20)
            self.assertEqual(handler.send_json.call_args.args[0], {"ok": True, "comments": [], "total": 0})
            provider.side_effect = netease_music.MusicBoxError("暂时不可用")
            Handler.serve_netease_catalog(handler, "/api/netease/comments", {"id": ["123"]})
            self.assertEqual(handler.send_json.call_args.args[1], 502)

    def test_online_lyrics_request_uses_only_the_song_id_and_caches_the_result(self):
        music = netease_music.MusicBox()
        text = "[00:01.20]第一句"
        payload = {"code": 200, "lrc": {"lyric": text + "\n"}}
        with mock.patch("netease_music._read_json", return_value=payload) as source:
            for _ in range(2):
                self.assertEqual(music.lyrics("123"), text)
        source.assert_called_once()
        request = source.call_args.args[0]
        self.assertEqual(request.full_url, "https://music.163.com/api/song/lyric?id=123&lv=1&kv=1&tv=-1")
        self.assertIsNone(request.get_header("Cookie"))

    def test_online_lyrics_caches_empty_results_and_backs_off_provider_restrictions(self):
        music = netease_music.MusicBox()
        blocked = HTTPError("", 429, "Too Many Requests", {}, io.BytesIO())
        with mock.patch("netease_music._HTTP.open", side_effect=[
            io.BytesIO(b'{"code":200,"nolyric":true}'), blocked, io.BytesIO(b'{"code":200}'),
        ]) as source, mock.patch("netease_music.time.monotonic", return_value=100) as clock:
            self.assertEqual(music.lyrics("123"), "")
            for value in ("456", "789"):
                with self.assertRaisesRegex(netease_music.MusicBoxError, "暂时限制"):
                    music.lyrics(value)
            self.assertTrue(blocked.fp.closed)
            self.assertEqual(music.lyrics("123"), "")
            self.assertEqual(source.call_count, 2)
            clock.return_value = 401
            self.assertEqual(music.lyrics("789"), "")
            self.assertEqual(source.call_count, 3)

    def test_online_lyrics_endpoint_uses_the_provider_instead_of_the_local_library(self):
        handler = mock.Mock()
        handler.path = "/api/netease/lyrics?id=123"
        with mock.patch("server.find_library_item") as local, mock.patch("server.NETEASE.lyrics") as provider:
            provider.return_value = "[00:01.20]歌词"
            Handler.handle_get(handler)
            handler.authorize_api_request.assert_called_once_with("GET", "/api/netease/lyrics")
            handler.serve_netease_catalog.assert_called_once_with("/api/netease/lyrics", {"id": ["123"]})
            Handler.serve_netease_catalog(handler, "/api/netease/lyrics", {"id": ["123"]})
            provider.assert_called_once_with("123")
            handler.send_json.assert_called_once_with({"ok": True, "lyrics": "[00:01.20]歌词", "source": "网易云音乐"})
            Handler.serve_netease_catalog(handler, "/api/netease/lyrics", {"id": ["123/456"]})
            self.assertEqual(handler.send_json.call_args.args[1], 400)
            self.assertEqual(provider.call_count, 1)
            local.assert_not_called()

    def test_daily_and_private_radar_produce_playback_rows(self):
        music = netease_music.MusicBox()
        rows = [{"song_id": 123, "name": "Song", "duration_ms": 63000}]
        with mock.patch("netease_music._request", side_effect=[{"logged_in": True, "user_id": 1}, rows, rows]) as source:
            self.assertEqual(music.daily()[0]["id"], "netease:123")
            self.assertEqual(music.radar()[0]["duration"], 63)
        self.assertEqual(source.call_args_list, [
            mock.call("/auth/status"),
            mock.call("/recommend/songs", limit=30),
            mock.call("/user/playlists/3136952023/tracks"),
        ])

    def test_private_radar_checks_login_before_reading_tracks(self):
        music = netease_music.MusicBox()
        with mock.patch("netease_music._read_json", return_value={"ok": True, "logged_in": False}) as source:
            with self.assertRaisesRegex(netease_music.MusicBoxError, "登录网易云"):
                music.radar()
        source.assert_called_once()
        self.assertTrue(source.call_args.args[0].endswith("/auth/status"))

    def test_account_recommended_playlists_use_top_level_items_and_valid_ids(self):
        music = netease_music.MusicBox()
        cover = "https://p1.music.126.net/example.jpg"
        playlist_payload = {"ok": True, "items": [
            {"id": "456", "name": "Recommendation", "cover_url": cover, "track_count": 65},
            {"id": "789", "name": "Unknown", "cover_url": "https://example.com/cover", "track_count": True},
            {"id": "456/tracks", "name": "Invalid"},
        ]}
        track_payload = {"ok": True, "data": [{"id": 123, "name": "Track"}]}
        with mock.patch("netease_music._read_json", side_effect=[playlist_payload, track_payload]) as source:
            self.assertEqual(music.recommended_playlists(), [
                {"id": "456", "name": "Recommendation", "cover_url": cover, "track_count": 65},
                {"id": "789", "name": "Unknown", "cover_url": "", "track_count": None},
            ])
            self.assertEqual(music.recommended_playlist("456")[0]["id"], "netease:123")
        self.assertTrue(source.call_args_list[0].args[0].endswith("/public-playlists?channel=account_recommend"))
        self.assertTrue(source.call_args_list[1].args[0].endswith("/public-playlists/456/tracks"))

    def test_recommended_playlist_endpoint_rejects_an_invalid_id(self):
        handler = mock.Mock()
        with mock.patch("server.NETEASE.recommended_playlist") as playlist:
            Handler.serve_netease_catalog(handler, "/api/netease/recommended-playlists/123/456", {})
            self.assertEqual(handler.send_json.call_args.args[1], 400)
            playlist.assert_not_called()

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
        handler = mock.Mock(mobile_proxy=False)
        with mock.patch("server.NETEASE.open_stream") as stream:
            for host, headers in (("192.168.1.2", {}), ("127.0.0.1", {"X-Fnaudio-Internal": "1"})):
                handler.client_address = (host, 2399)
                handler.headers = headers
                Handler.serve_netease_stream(handler, "123")
                self.assertEqual(handler.send_json.call_args.args[1], 403)
            stream.assert_not_called()

    def test_verified_provider_range_is_advertised_to_mpd(self):
        for requested, offset, response_code in (("", 0, 200), ("bytes=2-", 2, 206)):
            handler = mock.Mock(mobile_proxy=False, client_address=("127.0.0.1", 2399), headers={"Range": requested})
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
