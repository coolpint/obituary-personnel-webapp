"""Minimal private snapshots; no completed article text, tokens or recipient IDs."""
from __future__ import annotations

import hashlib
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .store import Store

MAX_BYTES = 1024 * 1024


def snapshot(store: Store) -> dict:
    conn = store._conn
    history = {(r["guid_hash"], r["channel"]): r["status"] for r in conn.execute("SELECT * FROM delivery_history")}
    active = []
    for row in conn.execute("SELECT i.*, a.channel, a.status, a.error, a.sent_at FROM alerts a JOIN items i ON i.id=a.item_id"):
        if row["status"] in ("sent", "skipped_legacy"):
            history[(hashlib.sha256(row["guid"].encode()).hexdigest(), row["channel"])] = row["status"]
        else:
            result = conn.execute("SELECT * FROM classifications WHERE item_id=?", (row["id"],)).fetchone()
            if result is None:
                raise ValueError("Active delivery has no frozen classification")
            active.append({
                "item": {k: row[k] for k in ("guid", "title", "summary", "link", "published_at")},
                "classification": {k: result[k] for k in (
                    "category", "matched_category_keywords", "media_score", "is_media_related", "matched_roles", "matched_outlets")},
                "channel": row["channel"], "status": row["status"], "error": row["error"], "sent_at": row["sent_at"],
            })
    value = {
        "version": 1,
        "history": [[guid_hash, channel, status] for (guid_hash, channel), status in sorted(history.items())],
        "active": active,
        "slots": [dict(r) for r in conn.execute("SELECT * FROM collection_slots ORDER BY slot DESC LIMIT 60")],
        "notifications": [dict(r) for r in conn.execute("SELECT * FROM system_notifications ORDER BY name DESC LIMIT 120")],
    }
    if len(json.dumps(value, ensure_ascii=True).encode()) > MAX_BYTES:
        raise ValueError("Private state size limit reached; refusing history eviction")
    return value


def restore(value: dict, path: Path) -> Store:
    if value.get("version") != 1 or not all(isinstance(value.get(k), list) for k in ("history", "active", "slots", "notifications")):
        raise ValueError("Invalid private state")
    if path.exists():
        raise ValueError("Refusing to overwrite local state")
    store = Store(path)
    try:
        with store._conn as conn:
            conn.executemany("INSERT INTO delivery_history VALUES (?,?,?)", value["history"])
            conn.executemany(
                "INSERT INTO collection_slots(slot,status,run_id,started_at,completed_at,error) VALUES (:slot,:status,:run_id,:started_at,:completed_at,:error)",
                value["slots"],
            )
            conn.executemany("INSERT INTO system_notifications(name,status) VALUES (:name,:status)", value["notifications"])
        from .rss_fetcher import RSSItem
        for entry in value["active"]:
            item_id = store.upsert_item(RSSItem(**entry["item"], raw_xml=""))
            result = entry["classification"]
            store.save_classification(item_id, result["category"], json.loads(result["matched_category_keywords"]),
                                      result["media_score"], bool(result["is_media_related"]),
                                      json.loads(result["matched_roles"]), json.loads(result["matched_outlets"]))
            store.mark_alert(item_id, entry["channel"], entry["status"], entry["error"])
            with store._conn:
                store._conn.execute("UPDATE alerts SET sent_at=? WHERE item_id=? AND channel=?",
                                    (entry["sent_at"], item_id, entry["channel"]))
        return store
    except BaseException:
        store.close()
        path.unlink(missing_ok=True)
        raise


class PrivateCheckpoint:
    """OIDC-authenticated HTTPS only; no durable state credential is introduced."""
    def __init__(self, endpoint: str, audience: str, session=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".workers.dev") or parsed.path != "/state" or parsed.query or parsed.fragment:
            raise ValueError("Expected approved workers.dev /state endpoint")
        if audience != endpoint:
            raise ValueError("OIDC audience must bind the exact state endpoint")
        self.endpoint, self.audience = endpoint, audience
        self.session = session or requests.Session()
        self.revision = None
        self.store = None
        self._bearer = None
        self._expires = 0

    def token(self) -> str:
        if self._bearer and self._expires > time.time() + 60:
            return self._bearer
        parts = urlsplit(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
        if parts.scheme != "https" or not parts.hostname or not parts.hostname.endswith(".actions.githubusercontent.com"):
            raise ValueError("Unexpected GitHub OIDC endpoint")
        query = dict(parse_qsl(parts.query))
        query["audience"] = self.audience
        url = urlunsplit(parts._replace(query=urlencode(query)))
        response = self.session.get(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
                                    timeout=15, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("OIDC request failed")
        value = response.json()["value"]
        # This is only a refresh hint for a token received over trusted HTTPS;
        # all authorization/signature checks remain on the Worker.
        try:
            payload = value.split('.')[1]
            expiry = float(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))["exp"])
        except (ValueError, KeyError, IndexError, TypeError):
            expiry = 0
        self._bearer, self._expires = value, expiry
        return value

    def request(self, action: str, **payload) -> dict:
        try:
            response = self.session.post(self.endpoint, json={"action": action, **payload},
                                         headers={"Authorization": "Bearer " + self.token()},
                                         timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError("Private state request rejected")
            return response.json()
        except Exception:
            # No raw HTTP error, token, response body or payload may reach logs.
            raise RuntimeError("Private state request failed") from None

    def open(self, path: Path) -> Store:
        value = self.request("acquire")
        self.revision = value["revision"]
        self.store = restore(value["snapshot"], path)
        self.store.checkpoint = self
        return self.store

    def __call__(self) -> None:
        value = self.request("checkpoint", revision=self.revision, snapshot=snapshot(self.store))
        self.revision = value["revision"]

    def release(self) -> None:
        self.request("release")
