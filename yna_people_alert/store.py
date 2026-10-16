from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

from .classifier import ClassificationResult
from .rss_fetcher import RSSItem


# User approval: 2026-10-16 10:25 KST. SQLite sent_at uses UTC.
RETRY_START_UTC = "2026-10-16 01:25:00"


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        self._conn.close()

    def _init_db(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                summary TEXT,
                link TEXT,
                published_at TEXT,
                raw_xml TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS classifications (
                item_id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                matched_category_keywords TEXT NOT NULL,
                media_score INTEGER NOT NULL,
                is_media_related INTEGER NOT NULL,
                matched_roles TEXT NOT NULL,
                matched_outlets TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(item_id, channel),
                FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
            );
            """
        )
        # Keep historical failures as explicit exclusions; never replay them. This
        # is idempotent, including when an older workflow cache is restored.
        self._conn.execute(
            """UPDATE alerts SET status = 'skipped_legacy'
               WHERE status = 'failed' AND datetime(sent_at) < datetime(?)""",
            (RETRY_START_UTC,),
        )
        self._conn.commit()

    def upsert_item(self, item: RSSItem) -> int:
        self._conn.execute(
            """
            INSERT INTO items(guid, title, summary, link, published_at, raw_xml)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(guid) DO UPDATE SET
                title=excluded.title,
                summary=excluded.summary,
                link=excluded.link,
                published_at=excluded.published_at,
                raw_xml=excluded.raw_xml
            """,
            (item.guid, item.title, item.summary, item.link, item.published_at, item.raw_xml),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT id FROM items WHERE guid = ?", (item.guid,)).fetchone()
        if row is None:
            raise RuntimeError(f"could not load item id for guid: {item.guid}")
        return int(row["id"])

    def save_classification(
        self,
        item_id: int,
        category: str,
        matched_category_keywords: Sequence[str],
        media_score: int,
        is_media_related: bool,
        matched_roles: Sequence[str],
        matched_outlets: Sequence[str],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO classifications(
                item_id,
                category,
                matched_category_keywords,
                media_score,
                is_media_related,
                matched_roles,
                matched_outlets
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                category=excluded.category,
                matched_category_keywords=excluded.matched_category_keywords,
                media_score=excluded.media_score,
                is_media_related=excluded.is_media_related,
                matched_roles=excluded.matched_roles,
                matched_outlets=excluded.matched_outlets
            """,
            (
                item_id,
                category,
                json.dumps(list(matched_category_keywords), ensure_ascii=False),
                media_score,
                int(is_media_related),
                json.dumps(list(matched_roles), ensure_ascii=False),
                json.dumps(list(matched_outlets), ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def is_alerted(self, item_id: int, channel: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM alerts WHERE item_id = ? AND channel = ? AND status = 'sent' LIMIT 1",
            (item_id, channel),
        ).fetchone()
        return row is not None

    def has_alert_history(self, item_id: int, channels: Sequence[str]) -> bool:
        if not channels:
            return False
        placeholders = ",".join("?" for _ in channels)
        row = self._conn.execute(
            f"SELECT 1 FROM alerts WHERE item_id = ? AND channel IN ({placeholders}) LIMIT 1",
            (item_id, *channels),
        ).fetchone()
        return row is not None

    def queue_alerts(self, item_id: int, channels: Sequence[str]) -> None:
        # Snapshot all intended channels before sending any. Existing histories
        # (including legacy failures) must not acquire newly configured channels.
        live_channels = [channel for channel in channels if channel != "console"]
        if self.has_alert_history(item_id, live_channels):
            return
        with self._conn:
            self._conn.executemany(
                """INSERT INTO alerts(item_id, channel, status)
                   VALUES (?, ?, 'pending') ON CONFLICT(item_id, channel) DO NOTHING""",
                [(item_id, channel) for channel in channels],
            )

    def pending_alerts(self):
        # Read stored payloads, not the current RSS window or a new classification.
        rows = self._conn.execute(
            """SELECT i.*, c.category, c.matched_category_keywords, c.media_score,
                      c.is_media_related, c.matched_roles, c.matched_outlets, a.channel
               FROM alerts a JOIN items i ON i.id = a.item_id
               JOIN classifications c ON c.item_id = i.id
               WHERE a.status IN ('pending', 'failed') ORDER BY i.id, a.channel"""
        ).fetchall()
        for row in rows:
            item = RSSItem(**{key: row[key] for key in (
                "guid", "title", "summary", "link", "published_at", "raw_xml"
            )})
            result = ClassificationResult(
                category=row["category"],
                matched_category_keywords=json.loads(row["matched_category_keywords"]),
                media_score=row["media_score"],
                is_media_related=bool(row["is_media_related"]),
                matched_roles=json.loads(row["matched_roles"]),
                matched_outlets=json.loads(row["matched_outlets"]),
            )
            yield int(row["id"]), item, result, row["channel"]

    def mark_alert(self, item_id: int, channel: str, status: str, error: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO alerts(item_id, channel, status, error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(item_id, channel) DO UPDATE SET
                status=excluded.status,
                error=excluded.error,
                sent_at=CURRENT_TIMESTAMP
            """,
            (item_id, channel, status, error),
        )
        self._conn.commit()
