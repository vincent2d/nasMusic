from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import netease_catalog_cache
import netease_music
from netease_catalog_cache import CatalogCache


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = CatalogCache()
        self.addCleanup(self.cache.close)

    def test_empty_results_are_cached_and_expire_without_sliding_on_reads(self):
        loader = mock.Mock(return_value=[])
        with mock.patch("netease_catalog_cache.time.time", return_value=100):
            self.assertEqual(self.cache.get_or_load("song", "search", loader, 200), [])
        with mock.patch("netease_catalog_cache.time.time", return_value=199):
            self.assertEqual(self.cache.get_or_load("song", "search", loader, 299), [])
            loader.assert_called_once()
        with mock.patch("netease_catalog_cache.time.time", return_value=200):
            self.cache.get_or_load("song", "search", loader, 300)
            self.assertEqual(loader.call_count, 2)

    def test_failed_load_is_retryable_and_returned_values_are_independent(self):
        loader = mock.Mock(side_effect=netease_music.MusicBoxError("unavailable"))
        with self.assertRaises(netease_music.MusicBoxError):
            self.cache.get_or_load("song", "search", loader, 10**12)
        loader.side_effect = None
        loader.return_value = [{"name": "Song"}]
        rows = self.cache.get_or_load("song", "search", loader, 10**12)
        rows[0]["name"] = "Changed"
        self.assertEqual(self.cache.get_or_load("song", "search", loader, 10**12), [{"name": "Song"}])
        self.assertEqual(loader.call_count, 2)

    def test_search_limit_evicts_least_recently_read_and_does_not_evict_recommendations(self):
        with mock.patch("netease_catalog_cache.MAX_SEARCH_ENTRIES", 2):
            for key, kind in (("daily", "recommendation"), ("a", "search"), ("b", "search")):
                self.cache.get_or_load(key, kind, lambda: [], 10**12)
            self.cache.get_or_load("a", "search", mock.Mock(), 10**12)
            self.cache.get_or_load("c", "search", lambda: [], 10**12)
        keys = {row[0] for row in self.cache._db.execute("SELECT key FROM catalog")}
        self.assertEqual(keys, {"daily", "a", "c"})

    def test_persistent_cache_survives_restart_and_expired_rows_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            first = CatalogCache(str(path))
            with mock.patch("netease_catalog_cache.time.time", return_value=100):
                first.get_or_load("a", "search", lambda: [{"name": "歌曲"}], 200)
            first.close()
            second = CatalogCache(str(path))
            self.addCleanup(second.close)
            loader = mock.Mock()
            with mock.patch("netease_catalog_cache.time.time", return_value=150):
                self.assertEqual(second.get_or_load("a", "search", loader, 300), [{"name": "歌曲"}])
                loader.assert_not_called()
            with mock.patch("netease_catalog_cache.time.time", return_value=200):
                second.get_or_load("b", "search", lambda: [], 300)
            self.assertEqual(second._db.execute("SELECT key FROM catalog").fetchall(), [("b",)])

    def test_disk_and_payload_budgets_hold_after_large_entries_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            cache = CatalogCache(str(path))
            self.addCleanup(cache.close)
            for index in range(18):
                cache.get_or_load(str(index), "search", lambda: ["x" * (1900 * 1024)], 10**12)
            count, size = cache._db.execute("SELECT COUNT(*), SUM(length(payload)) FROM catalog").fetchone()
            self.assertLess(count, 18)
            self.assertLessEqual(size, netease_catalog_cache._PAYLOAD_BUDGET)
            self.assertLessEqual(path.stat().st_size, netease_catalog_cache.MAX_CACHE_BYTES)
            self.assertEqual(cache._db.execute("PRAGMA freelist_count").fetchone()[0], 0)
            loader = mock.Mock(return_value=["x" * netease_catalog_cache.MAX_PAYLOAD_BYTES])
            for _ in range(2):
                cache.get_or_load("oversized", "search", loader, 10**12)
            self.assertEqual(loader.call_count, 2)

    def test_same_key_loads_once_while_another_cached_key_is_available(self):
        self.cache.get_or_load("warm", "search", lambda: [], 10**12)
        entered, release = threading.Event(), threading.Event()

        def load():
            entered.set()
            self.assertTrue(release.wait(3))
            return [{"name": "Song"}]

        loader = mock.Mock(side_effect=load)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.cache.get_or_load, "cold", "search", loader, 10**12)
            self.assertTrue(entered.wait(3))
            second = pool.submit(self.cache.get_or_load, "cold", "search", loader, 10**12)
            try:
                self.assertEqual(pool.submit(self.cache.get_or_load, "warm", "search", mock.Mock(), 10**12).result(2), [])
            finally:
                release.set()
            self.assertEqual(first.result(3), second.result(3))
        loader.assert_called_once()

    def test_database_failures_do_not_prevent_fresh_results(self):
        with mock.patch.object(self.cache, "_get", side_effect=sqlite3.DatabaseError("unreadable")), \
                mock.patch.object(self.cache, "_put", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(self.cache.get_or_load("song", "search", lambda: [{"name": "Song"}], 10**12),
                             [{"name": "Song"}])
        self.assertFalse(self.cache._pending)


class NeteasePreloadTests(unittest.TestCase):
    def setUp(self):
        self.music = netease_music.MusicBox()
        self.addCleanup(lambda: self.music._catalog.close())
        self.rows = [{"song_id": 123, "name": "Song", "duration_ms": 12000,
                      "url": "https://example.com/audio", "cookie": "private"}]

    def test_search_uses_24_hour_cache_and_persists_only_catalog_fields(self):
        with mock.patch("netease_music.time.time", return_value=100), \
                mock.patch("netease_music._request", return_value=self.rows) as source:
            self.music.search(" Song ")
            self.music.search("Song")
            source.assert_called_once()
            expires, payload = self.music._catalog._db.execute("SELECT expires, payload FROM catalog").fetchone()
            self.assertEqual(expires, 86500)
            self.assertNotIn(b"cookie", payload)
            self.assertNotIn(b"example.com", payload)
            self.music._items.clear()
            self.assertEqual(self.music.search("Song")[0]["title"], "Song")
            self.assertEqual(self.music.track("netease:123")["duration"], 12)
        with mock.patch("netease_music.time.time", return_value=86500), \
                mock.patch("netease_music._request", return_value=self.rows) as source:
            self.music.search("Song")
            source.assert_called_once()

    def test_daily_refreshes_at_0605_china_time_and_radar_after_one_hour(self):
        clock = lambda hour, minute: datetime(2026, 11, 19, hour, minute, tzinfo=netease_music._CHINA_TIME).timestamp()
        with mock.patch.object(self.music, "_account_id", return_value="1"), \
                mock.patch("netease_music._request", return_value=self.rows) as source:
            with mock.patch("netease_music.time.time", return_value=clock(5, 0)):
                self.music.daily()
                self.music.radar()
            with mock.patch("netease_music.time.time", return_value=clock(5, 59)):
                self.music.daily()
                self.music.radar()
                self.assertEqual(source.call_count, 2)
            with mock.patch("netease_music.time.time", return_value=clock(6, 0)):
                self.music.radar()
                self.assertEqual(source.call_count, 3)
            with mock.patch("netease_music.time.time", return_value=clock(6, 5)):
                self.music.daily()
                self.assertEqual(source.call_count, 4)
            with mock.patch("netease_music.time.time", return_value=clock(23, 59)):
                self.music.daily()
                self.assertEqual(source.call_count, 4)

    def test_account_is_rechecked_after_one_minute_and_new_account_gets_own_recommendation(self):
        def request(path, **kwargs):
            if path == "/auth/status":
                return {"logged_in": True, "user_id": account[0]}
            return self.rows

        account = [1]
        with mock.patch("netease_music._request", side_effect=request) as source:
            with mock.patch("netease_music.time.monotonic", return_value=100):
                self.music.daily()
                self.music.daily()
                self.assertEqual(source.call_count, 2)
            account[0] = 2
            with mock.patch("netease_music.time.monotonic", return_value=160):
                self.music.daily()
                self.assertEqual(source.call_count, 4)
            with mock.patch("netease_music.time.monotonic", return_value=220), \
                    mock.patch("netease_music._request", return_value={"logged_in": False}):
                with self.assertRaisesRegex(netease_music.MusicBoxError, "登录网易云"):
                    self.music.daily()

    def test_background_preloads_both_lists_and_stops_without_waiting_for_next_tick(self):
        ready = threading.Event()

        def request(path, **kwargs):
            if path == "/auth/status":
                return {"logged_in": True, "user_id": 1}
            if "playlists" in path:
                ready.set()
            return self.rows

        with tempfile.TemporaryDirectory() as directory, mock.patch("netease_music._request", side_effect=request):
            self.music.start_catalog_cache({"cache_dir": directory})
            try:
                self.assertTrue(ready.wait(3))
            finally:
                self.music.stop_catalog_cache()
            self.assertFalse(self.music._catalog_thread.is_alive())
            self.assertEqual(self.music._catalog._db.execute("SELECT COUNT(*) FROM catalog").fetchone()[0], 2)
            self.assertTrue((Path(directory) / "netease" / "catalog.sqlite3").is_file())

    def test_unwritable_cache_directory_keeps_memory_cache_and_background_preload(self):
        ready = threading.Event()
        with mock.patch("netease_music.os.makedirs", side_effect=PermissionError("read only")), \
                mock.patch.object(self.music, "_account_id", return_value="1"), \
                mock.patch.object(self.music, "_recommendation", side_effect=lambda *args: ready.set()):
            self.music.start_catalog_cache({"cache_dir": "/unwritable"})
            try:
                self.assertTrue(ready.wait(3))
            finally:
                self.music.stop_catalog_cache()
        with mock.patch("netease_music._request", return_value=self.rows) as source:
            self.music.search("Song")
            self.music.search("Song")
            source.assert_called_once()

    def test_preload_retries_a_failed_daily_without_skipping_radar(self):
        self.music._catalog_stop = mock.Mock()
        self.music._catalog_stop.is_set.side_effect = [False, False, False, False, False, False, True]
        with mock.patch.object(self.music, "_account_id", return_value="1"), \
                mock.patch.object(self.music, "_recommendation", side_effect=[netease_music.MusicBoxError("offline"), [], [], []]) as load:
            self.music._catalog_loop()
        self.assertEqual(load.call_args_list, [mock.call("daily", "1"), mock.call("radar", "1")] * 2)
        self.assertEqual(self.music._catalog_stop.wait.call_args_list, [mock.call(60), mock.call(60)])


if __name__ == "__main__":
    unittest.main()
