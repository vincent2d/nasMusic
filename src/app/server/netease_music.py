"""Read-only access to the MusicBox service installed by fnmusic-ext."""

import json
import re
import threading
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

STREAM_PREFIX = "/internal/netease/"
MUSICBOX = "http://127.0.0.1:8770/api/v1"
_ID = re.compile(r"[1-9][0-9]{0,19}")
_MAX_PAYLOAD = 2 * 1024 * 1024
_MAX_CACHED_TRACKS = 2048
_HTTP = build_opener(ProxyHandler({}))


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


def _request(path, **parameters):
    query = "?" + urlencode(parameters) if parameters else ""
    try:
        with _HTTP.open(MUSICBOX + path + query, timeout=25) as response:
            raw = response.read(_MAX_PAYLOAD + 1)
        if len(raw) > _MAX_PAYLOAD:
            raise ValueError("response too large")
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise MusicBoxError("网易云音源暂时不可用") from exc
    if not isinstance(payload, dict):
        raise MusicBoxError("网易云音源返回了无效数据")
    if payload.get("error") == "not_logged_in" or payload.get("logged_in") is False:
        raise MusicBoxError("请在 fnMusic 扩展管理中登录网易云")
    if payload.get("ok") is not True:
        raise MusicBoxError("网易云音源请求失败")
    return payload.get("data")


def _text(value):
    return value.strip()[:512] if isinstance(value, str) else ""


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
                item = {
                    "id": track_id,
                    "title": _text(row.get("name")) or "网易云歌曲",
                    "artist": _text(row.get("artist")),
                    "album": _text(row.get("album_name")),
                    "duration": max(0, min(milliseconds, 86400000)) / 1000 if type(milliseconds) is int else 0,
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

    def playlists(self):
        rows = _request("/user/playlists", limit=100)
        if not isinstance(rows, list):
            raise MusicBoxError("网易云音源返回了无效的歌单列表")
        return [
            {"id": str(row["playlist_id"]), "name": _text(row.get("name")) or "网易云歌单"}
            for row in rows
            if isinstance(row, dict) and _ID.fullmatch(str(row.get("playlist_id") or ""))
        ]

    def playlist(self, playlist_id):
        return self._remember(_request(f"/user/playlists/{playlist_id}/tracks"))

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
