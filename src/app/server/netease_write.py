"""Verified account writes executed inside the existing MusicBox container."""

WRITE_SCRIPT = r'''
import json
import sys
import time
from NEMbox.api import NetEase


def playlist(api, pid):
    res = api.eapi_request("/api/v6/playlist/detail", {"id": int(pid), "n": 100000, "s": 0})
    pl = res.get("playlist")
    if res.get("code") != 200 or not isinstance(pl, dict) or str(pl.get("id")) != str(pid):
        raise ValueError("歌单读回失败")
    rows, count = pl.get("trackIds"), pl.get("trackCount")
    if not isinstance(rows, list) or type(count) is not int or count != len(rows):
        raise ValueError("歌单读回不完整")
    if any(not isinstance(row, dict) or type(row.get("id")) is not int for row in rows):
        raise ValueError("歌单曲目数据无效")
    return pl


def verify(read, matches):
    for attempt in range(3):
        result = read()
        if matches(result):
            return result
        if attempt < 2:
            time.sleep(0.5)
    raise ValueError("网易云读回结果与操作不一致，请刷新后确认")


def perform(api, operation, args, receipt):
    account = api.get_account_info().get("account") or {}
    uid = account.get("id")
    if not uid:
        return {"ok": False, "error": "not_logged_in"}
    if operation == "like":
        sid, desired = int(args[0]), args[1] == "true"
        receipt.update(song_id=str(sid), desired=desired)
        res = api.eapi_request("/api/song/like", {"trackId": sid, "userid": uid, "like": desired})
        receipt["code"] = res.get("code")
        if res.get("code") != 200:
            return {"ok": False, "message": "网易云未接受红心操作，请刷新后重试。"}

        def read_likes():
            data = api.request("POST", "/weapi/song/like/get", {"uid": uid})
            ids = data.get("ids")
            if data.get("code") != 200 or not isinstance(ids, list) or any(type(x) is not int for x in ids):
                raise ValueError("红心列表读回失败")
            return ids

        verify(read_likes, lambda ids: (sid in ids) == desired)
        return {"ok": True, "verified": True}
    if operation == "create":
        name, private = args[0], args[1] == "1"
        res = api.eapi_request("/api/playlist/create", {
            "name": name, "privacy": 10 if private else 0, "type": "NORMAL",
        })
        receipt["code"] = res.get("code")
        created = res.get("playlist") or {}
        pid = res.get("id") or created.get("id")
        if res.get("code") != 200 or not pid:
            return {"ok": False, "message": "网易云未确认创建歌单，请刷新歌单列表后确认。"}
        receipt["playlist_id"] = str(pid)
        pl = verify(lambda: playlist(api, pid), lambda p: p.get("name") == name)
        return {"ok": True, "verified": True, "id": str(pid), "name": pl["name"],
                "cover_url": pl.get("coverImgUrl") or "", "track_count": pl["trackCount"],
                "private": pl.get("privacy") == 10}
    op, pid, sid = args[0], int(args[1]), int(args[2])
    receipt.update(playlist_id=str(pid), song_id=str(sid), desired=op == "add")
    res = api.eapi_request("/api/playlist/manipulate/tracks", {
        "op": op, "pid": pid, "trackIds": json.dumps([sid]), "imme": "true",
    })
    code = res.get("code")
    receipt["code"] = code
    # A duplicate-add response is accepted only after confirming membership.
    if code != 200 and not (code == 502 and op == "add"):
        return {"ok": False, "message": "网易云未接受歌单操作，请刷新后重试。"}
    pl = verify(lambda: playlist(api, pid),
                lambda p: (sid in [row["id"] for row in p["trackIds"]]) == (op == "add"))
    return {"ok": True, "verified": True, "already_exists": code == 502,
            "count": pl["trackCount"], "cover_url": pl.get("coverImgUrl") or ""}


operation, args = sys.argv[1], sys.argv[2:]
receipt = {"operation": operation, "time": int(time.time())}
try:
    api = NetEase()
    # Disable requests-cache for both the mutation and its independent readback.
    with api.session.cache_disabled():
        result = perform(api, operation, args, receipt)
except Exception as exc:
    receipt["error_type"] = type(exc).__name__
    result = {"ok": False, "message": "网易云操作结果未确认，请刷新后检查；新建歌单请勿重复提交。"}
receipt["verified"] = result.get("verified") is True
result["receipt"] = receipt
print(json.dumps(result, ensure_ascii=False))
'''
