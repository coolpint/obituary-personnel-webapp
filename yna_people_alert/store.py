from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

from .rss_fetcher import RSSItem


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
            "SELECT 1 FROM alerts WHERE item_id = ? AND channel = ? LIMIT 1",
            (item_id, channel),
        ).fetchone()
        return row is not None

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

