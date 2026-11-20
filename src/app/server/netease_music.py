"""NetEase catalog, playback and account favorites through MusicBox."""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from metadata_provider_response import lyrics_document
from netease_catalog_cache import CatalogCache
from netease_write import WRITE_SCRIPT

STREAM_PREFIX = "/internal/netease/"
MUSICBOX = "http://127.0.0.1:8770/api/v1"
_RADAR_PLAYLIST_ID = "3136952023"
_ID = re.compile(r"[1-9][0-9]{0,19}")
_MAX_PAYLOAD = 2 * 1024 * 1024
_MAX_CACHED_TRACKS = 2048
_MAX_CACHED_LYRICS = 128
_LYRICS_RESTRICTIONS = (403, 418, 429)
_CHINA_TIME = timezone(timedelta(hours=8))
_HTTP = build_opener(ProxyHandler({}))
_LIKE_COMMAND = (
    "/usr/bin/docker", "exec", "fnmusic-sources", "python", "-c", WRITE_SCRIPT, "like",
)


class MusicBoxError(RuntimeError):
    pass


def song_id(track_id):
    if isinstance(track_id, str) and track_id.startswith("netease:"):
        value = track_id.removeprefix("netease:")
        if _ID.fullmatch(value):
            return value
    return ""


def stream_uri(track_id, port):
    value = song_id(track_id)
    return f"http://127.0.0.1:{port}{STREAM_PREFIX}{value}" if value and type(port) is int and 1024 <= port <= 65535 else ""


def _read_json(request, timeout, error):
    try:
        with _HTTP.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_PAYLOAD + 1)
        if len(raw) > _MAX_PAYLOAD:
            raise ValueError("response too large")
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        raise MusicBoxError(error) from exc


def _request(path, *, result_key="data", **parameters):
    query = "?" + urlencode(parameters) if parameters else ""
    payload = _read_json(MUSICBOX + path + query, 25, "网易云音源暂时不可用")
    if not isinstance(payload, dict):
        raise MusicBoxError("网易云音源返回了无效数据")
    if payload.get("error") == "not_logged_in" or payload.get("logged_in") is False:
        raise MusicBoxError("请在 fnMusic 扩展管理中登录网易云")
    if payload.get("ok") is not True:
        raise MusicBoxError("网易云音源请求失败")
    return payload.get(result_key)


def _text(value):
    return value.strip()[:512] if isinstance(value, str) else ""


def _playlist_summaries(rows, id_key):
    if not isinstance(rows, list):
        raise MusicBoxError("网易云音源返回了无效的歌单列表")
    return [
        {
            "id": str(row[id_key]),
            "name": _text(row.get("name")) or "网易云歌单",
            "cover_url": _netease_url(row.get("cover_url")),
            "track_count": row["track_count"]
            if type(row.get("track_count")) is int and row["track_count"] >= 0 else None,
        }
        for row in rows
        if isinstance(row, dict) and _ID.fullmatch(str(row.get(id_key) or ""))
    ]


def _netease_url(value):
    if not isinstance(value, str) or len(value) > 4096 or not value.isascii() or any(ord(c) <= 32 for c in value):
        return ""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme in ("https", "http") and not url.username and not url.password
                and url.port in (None, 80, 443)
                and host.endswith((".music.126.net", ".music.163.com"))):
            return value
    except ValueError:
        pass
    return ""


class _MediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _netease_url(newurl):
            raise MusicBoxError("网易云音频地址验证失败")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_MEDIA_HTTP = build_opener(ProxyHandler({}), _MediaRedirect())


class MusicBox:
    def __init__(self):
        self._items = OrderedDict()
        self._lock = threading.Lock()
        self._lyrics = OrderedDict()
        self._lyrics_lock = threading.Lock()
        self._lyrics_next_request = 0
        self._lyrics_restricted = False
        self._catalog = CatalogCache()
        self._catalog_stop = threading.Event()
        self._catalog_thread = None
        self._account_lock = threading.Lock()
        self._account_user = ""
        self._account_expires = 0

    def start_catalog_cache(self, config):
        directory = os.path.join(config["cache_dir"], "netease")
        try:
            os.makedirs(directory, exist_ok=True)
            cache = CatalogCache(os.path.join(directory, "catalog.sqlite3"))
        except (OSError, sqlite3.Error) as exc:
            print("fnAudio NetEase disk cache unavailable: %s" % exc, flush=True)
        else:
            self._catalog.close()
            self._catalog = cache
        self._catalog_stop.clear()
        self._catalog_thread = threading.Thread(target=self._catalog_loop, name="fnaudio-netease-preload", daemon=True)
        self._catalog_thread.start()

    def stop_catalog_cache(self):
        self._catalog_stop.set()
        if self._catalog_thread is not None:
            self._catalog_thread.join(timeout=30)

    def _catalog_loop(self):
        while not self._catalog_stop.is_set():
            try:
                account = self._account_id()
            except MusicBoxError as exc:
                print("fnAudio NetEase preload unavailable: %s" % exc, flush=True)
            else:
                for kind in ("daily", "radar"):
                    if self._catalog_stop.is_set():
                        return
                    try:
                        self._recommendation(kind, account)
                    except MusicBoxError as exc:
                        print("fnAudio NetEase %s preload failed: %s" % (kind, exc), flush=True)
            self._catalog_stop.wait(60)

    def _account_id(self):
        # Auth status invokes a MusicBox subprocess. Share its result briefly
        # to avoid running the CLI for every warm catalog request.
        with self._account_lock:
            if time.monotonic() < self._account_expires:
                return self._account_user
            self._account_user = ""
            status = _request("/auth/status")
            if not isinstance(status, dict) or status.get("logged_in") is not True:
                raise MusicBoxError("请在 fnMusic 扩展管理中登录网易云")
            value = str(status.get("user_id") or "")
            if not _ID.fullmatch(value):
                raise MusicBoxError("网易云音源返回了无效的账号信息")
            self._account_user = value
            self._account_expires = time.monotonic() + 60
            return value

    def _catalog_tracks(self, key, kind, loader, expires):
        def load():
            # Persist only the normalized catalog fields, never media URLs,
            # cookies, lyrics, artwork bytes or downloaded audio.
            return [{
                "song_id": song_id(item["id"]), "name": item["title"],
                "artist": item["artist"], "album_name": item["album"],
                "duration_ms": round(item["duration"] * 1000), "album_pic_url": item["cover_url"],
            } for item in self._remember(loader())]
        return self._remember(self._catalog.get_or_load(key, kind, load, expires))

    def _recommendation(self, kind, account):
        now = time.time()
        if kind == "daily":
            refresh = datetime.fromtimestamp(now, _CHINA_TIME).replace(hour=6, minute=5, second=0, microsecond=0)
            if refresh.timestamp() <= now:
                refresh += timedelta(days=1)
            expires = refresh.timestamp()
            loader = lambda: _request("/recommend/songs", limit=30)
        else:
            expires = now + 3600
            loader = lambda: _request(f"/user/playlists/{_RADAR_PLAYLIST_ID}/tracks")
        return self._catalog_tracks(f"{kind}:{account}", "recommendation", loader, expires)

    def _remember(self, rows):
        if not isinstance(rows, list):
            raise MusicBoxError("网易云音源返回了无效的曲目列表")
        result = []
        with self._lock:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = str(row.get("song_id") or row.get("id") or "")
                if not _ID.fullmatch(value):
                    continue
                track_id = "netease:" + value
                milliseconds = row.get("duration_ms")
                if type(milliseconds) is not int:
                    seconds = row.get("duration")
                    milliseconds = seconds * 1000 if type(seconds) is int else 0
                item = {
                    "id": track_id,
                    "title": _text(row.get("name")) or _text(row.get("song_name")) or "网易云歌曲",
                    "artist": _text(row.get("artist")),
                    "album": _text(row.get("album_name")),
                    "duration": max(0, min(milliseconds, 86400000)) / 1000,
                    "cover_url": _netease_url(row.get("album_pic_url")) or self._items.get(track_id, {}).get("cover_url", ""),
                    "format": "网易云",
                }
                result.append(item)
                self._items[track_id] = item
                self._items.move_to_end(track_id)
            while len(self._items) > _MAX_CACHED_TRACKS:
                self._items.popitem(last=False)
        return result

    def track(self, track_id):
        with self._lock:
            return self._items.get(track_id) or {"id": track_id, "title": "网易云歌曲", "format": "网易云"}

    def hydrate(self, track_ids):
        ids = list(dict.fromkeys(track_id for track_id in track_ids if song_id(track_id)))
        with self._lock:
            missing = [song_id(track_id) for track_id in ids if not self._items.get(track_id, {}).get("cover_url")]
        for start in range(0, len(missing), 100):
            try:
                self._remember(_request("/songs/detail", ids=",".join(missing[start:start + 100])))
            except MusicBoxError:
                break
        return [self.track(track_id) for track_id in ids]

    def search(self, keyword):
        keyword = keyword.strip()[:120]
        if not keyword:
            return []
        return self._catalog_tracks("search:" + keyword, "search",
                                    lambda: _request("/search", keyword=keyword, type="song", limit=50),
                                    time.time() + 86400)

    def similar(self, value):
        query = urlencode({"songid": value, "offset": 0, "total": "true", "limit": 20})
        request = Request(
            "https://music.163.com/api/discovery/simiSong?" + query,
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        error = "暂时无法获取网易云相似歌曲"
        payload = _read_json(request, 8, error)
        if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("songs"), list):
            raise MusicBoxError(error)
        ids = list(dict.fromkeys(
            str(row["id"]) for row in payload["songs"][:20]
            if isinstance(row, dict) and _ID.fullmatch(str(row.get("id"))) and str(row["id"]) != value
        ))
        if not ids:
            return []
        details = self._remember(_request("/songs/detail", ids=",".join(ids)))
        by_id = {song_id(item["id"]): item for item in details}
        return [by_id[value] for value in ids if value in by_id]

    def comments(self, value, offset=0):
        query = urlencode({"offset": offset, "limit": 20, "total": "true"})
        request = Request(
            f"https://music.163.com/api/v1/resource/comments/R_SO_4_{value}?{query}",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        error = "暂时无法获取网易云歌曲评论，请稍后重试。"
        payload = _read_json(request, 8, error)
        if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("comments"), list):
            raise MusicBoxError(error)

        def rows(comments):
            result = []
            for row in comments[:20]:
                if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                    continue
                user = row.get("user")
                likes, timestamp = row.get("likedCount"), row.get("time")
                result.append({
                    "user": _text(user.get("nickname")) if isinstance(user, dict) else "",
                    "content": row["content"][:10000],
                    "likes": likes if type(likes) is int and likes >= 0 else 0,
                    "time": timestamp if type(timestamp) is int and 0 <= timestamp <= 8640000000000000 else 0,
                })
            return result

        hot = payload.get("hotComments")
        total = payload.get("total")
        return {
            "comments": rows(payload["comments"]),
            "hot_comments": rows(hot) if offset == 0 and isinstance(hot, list) else [],
            "total": total if type(total) is int and total >= 0 else 0,
            "more": payload.get("more") is True,
            "offset": offset,
        }

    def lyrics(self, value):
        restricted = "网易云暂时限制歌词请求，请稍后重新打开歌词页。"
        unavailable = "暂时无法获取网易云歌词，请稍后重试。"
        with self._lyrics_lock:
            now = time.monotonic()
            cached = self._lyrics.pop(value, None)
            if cached and cached[0] > now:
                self._lyrics[value] = cached
                return cached[1]
            if now < self._lyrics_next_request:
                raise MusicBoxError(restricted if self._lyrics_restricted else unavailable)

            query = urlencode({"id": value, "lv": 1, "kv": 1, "tv": -1})
            request = Request("https://music.163.com/api/song/lyric?" + query,
                              headers={"Accept": "application/json"})
            try:
                payload = _read_json(request, 5, unavailable)
            except MusicBoxError as exc:
                cause = exc.__cause__
                if isinstance(cause, HTTPError):
                    cause.close()
                    payload = {"code": cause.code}
                else:
                    payload = {}

            code = payload.get("code") if isinstance(payload, dict) else None
            now = time.monotonic()
            self._lyrics_restricted = code in _LYRICS_RESTRICTIONS
            self._lyrics_next_request = now + (300 if self._lyrics_restricted else 10 if code != 200 else 0)
            if code != 200:
                raise MusicBoxError(restricted if self._lyrics_restricted else unavailable)

            lrc = payload.get("lrc")
            raw = lrc.get("lyric") if isinstance(lrc, dict) else None
            text = lyrics_document(raw, limit=64 * 1024)
            self._lyrics[value] = (now + (86400 if text else 600), text)
            if len(self._lyrics) > _MAX_CACHED_LYRICS:
                self._lyrics.popitem(last=False)
            return text

    def playlists(self):
        return _playlist_summaries(_request("/user/playlists", limit=100), "playlist_id")

    def playlist(self, playlist_id):
        return self._remember(_request(f"/user/playlists/{playlist_id}/tracks"))

    def daily(self):
        return self._recommendation("daily", self._account_id())

    def radar(self):
        return self._recommendation("radar", self._account_id())

    def recommended_playlists(self):
        rows = _request("/public-playlists", result_key="items", channel="account_recommend")
        return _playlist_summaries(rows, "id")

    def recommended_playlist(self, playlist_id):
        return self._remember(_request(f"/public-playlists/{playlist_id}/tracks"))

    def set_liked(self, value, liked):
        if not isinstance(value, str) or not _ID.fullmatch(value) or type(liked) is not bool:
            raise ValueError("invalid NetEase favorite")
        try:
            reply = subprocess.run(
                (*_LIKE_COMMAND, value, "true" if liked else "false"),
                capture_output=True, text=True, timeout=25, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MusicBoxError("网易云未确认收藏操作，请重试。") from exc
        try:
            result = json.loads(reply.stdout.strip()) if reply.returncode == 0 else {}
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        if isinstance(result.get("receipt"), dict):
            print("fnAudio NetEase write " + json.dumps(result["receipt"], ensure_ascii=False), flush=True)
        if result.get("ok") is True and result.get("verified") is True:
            return
        if result.get("error") == "not_logged_in":
            raise MusicBoxError("请在 fnMusic 扩展管理中登录网易云")
        raise MusicBoxError(_text(result.get("message")) or "网易云未确认收藏操作，请刷新后重试。")

    def stream(self, value):
        row = _request(f"/song/{value}/url", quality="exhigh")
        if isinstance(row, dict) and not (row.get("freeTrialInfo") or row.get("free_trial_info")):
            url = _netease_url(row.get("url"))
            if url:
                return url
        raise MusicBoxError("当前账号无法取得歌曲的完整音频")

    def open_stream(self, value, byte_range):
        request = Request(self.stream(value), headers={"Range": byte_range})
        return _MEDIA_HTTP.open(request, timeout=12)


MUSIC = MusicBox()
