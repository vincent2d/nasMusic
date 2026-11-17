"""NetEase catalog, playback and account favorites through MusicBox."""

import json
import re
import subprocess
import threading
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

STREAM_PREFIX = "/internal/netease/"
MUSICBOX = "http://127.0.0.1:8770/api/v1"
_RADAR_PLAYLIST_ID = "3136952023"
_ID = re.compile(r"[1-9][0-9]{0,19}")
_MAX_PAYLOAD = 2 * 1024 * 1024
_MAX_CACHED_TRACKS = 2048
_HTTP = build_opener(ProxyHandler({}))
_LIKE_COMMAND = (
    "/usr/bin/docker", "exec", "fnmusic-sources", "python", "-c", """
import sys
from NEMbox.api import NetEase

api = NetEase()
if not (api.get_account_info().get("account") or {}).get("id"):
    print("not_logged_in")
else:
    print("ok" if api.song_like(int(sys.argv[1]), like=sys.argv[2] == "true") else "rejected")
""",
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
                    "cover_url": _netease_url(row.get("album_pic_url")),
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
            missing = [song_id(track_id) for track_id in ids if track_id not in self._items]
        for start in range(0, len(missing), 100):
            try:
                self._remember(_request("/songs/detail", ids=",".join(missing[start:start + 100])))
            except MusicBoxError:
                break
        return [self.track(track_id) for track_id in ids]

    def search(self, keyword):
        keyword = keyword.strip()[:120]
        return self._remember(_request("/search", keyword=keyword, type="song", limit=50)) if keyword else []

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

    def playlists(self):
        return _playlist_summaries(_request("/user/playlists", limit=100), "playlist_id")

    def playlist(self, playlist_id):
        return self._remember(_request(f"/user/playlists/{playlist_id}/tracks"))

    def daily(self):
        return self._remember(_request("/recommend/songs", limit=30))

    def radar(self):
        _request("/auth/status")
        return self.playlist(_RADAR_PLAYLIST_ID)

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
        result = reply.stdout.strip()
        if reply.returncode == 0 and result == "ok":
            return
        if reply.returncode == 0 and result == "not_logged_in":
            raise MusicBoxError("请在 fnMusic 扩展管理中登录网易云")
        raise MusicBoxError("网易云未确认收藏操作，请重试。")

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
