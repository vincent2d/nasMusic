"""Bounded metadata-only cache for NetEase catalog responses."""

import json
import sqlite3
import threading
import time

MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_SEARCH_ENTRIES = 1000
# Leave room for SQLite pages, indexes and per-row overhead under the file cap.
_PAYLOAD_BUDGET = 24 * 1024 * 1024


class CatalogCache:
    def __init__(self, path=":memory:"):
        self._lock = threading.Condition()
        self._pending = set()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA auto_vacuum = FULL")
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        self._db.execute("PRAGMA max_page_count = %d" % (MAX_CACHE_BYTES // page_size))
        self._db.execute("""CREATE TABLE IF NOT EXISTS catalog (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, expires REAL NOT NULL,
            accessed REAL NOT NULL, payload BLOB NOT NULL)""")
        self._db.commit()

    def _get(self, key, now):
        with self._db:
            self._db.execute("DELETE FROM catalog WHERE expires <= ?", (now,))
            row = self._db.execute("SELECT payload FROM catalog WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE catalog SET accessed = ? WHERE key = ?", (now, key))
        return json.loads(row[0])

    def _put(self, key, kind, rows, expires):
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            return
        with self._db:
            self._db.execute("DELETE FROM catalog WHERE expires <= ?", (time.time(),))
            self._db.execute("INSERT OR REPLACE INTO catalog VALUES (?, ?, ?, ?, ?)",
                             (key, kind, expires, time.time(), payload))
            limit = MAX_SEARCH_ENTRIES if kind == "search" else 2
            self._db.execute("""DELETE FROM catalog WHERE key IN (
                SELECT key FROM catalog WHERE kind = ?
                ORDER BY accessed DESC, rowid DESC LIMIT -1 OFFSET ?)""", (kind, limit))
            size = self._db.execute("SELECT COALESCE(SUM(length(payload)), 0) FROM catalog").fetchone()[0]
            while size > _PAYLOAD_BUDGET:
                oldest, length = self._db.execute(
                    "SELECT key, length(payload) FROM catalog ORDER BY accessed, rowid LIMIT 1").fetchone()
                self._db.execute("DELETE FROM catalog WHERE key = ?", (oldest,))
                size -= length

    def get_or_load(self, key, kind, loader, expires):
        # Coalesce concurrent requests for the same key without holding the DB
        # lock during network I/O or blocking hits for other keys.
        with self._lock:
            while key in self._pending:
                self._lock.wait()
            try:
                cached = self._get(key, time.time())
            except (sqlite3.Error, ValueError) as exc:
                print("fnAudio NetEase cache read failed: %s" % exc, flush=True)
                cached = None
            if cached is not None:
                return cached
            self._pending.add(key)
        try:
            rows = loader()
            with self._lock:
                try:
                    self._put(key, kind, rows, expires)
                except sqlite3.Error as exc:
                    print("fnAudio NetEase cache write failed: %s" % exc, flush=True)
            return rows
        finally:
            with self._lock:
                self._pending.remove(key)
                self._lock.notify_all()

    def close(self):
        with self._lock:
            self._db.close()
