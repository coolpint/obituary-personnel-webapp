"""Approved, one-time migration from an exact cache; never collects or sends."""
import hashlib
import os
from pathlib import Path
import sqlite3

from .private_state import PrivateCheckpoint, snapshot
from .store import Store


def main():
    key = os.environ.get("APPROVED_CACHE_KEY", "")
    if not key or key != os.environ.get("MATCHED_CACHE_KEY"):
        raise ValueError("Exact reviewed cache required")
    path = Path("data/yna_people_alert.db")
    if not path.is_file():
        raise ValueError("Cache DB missing; refusing empty history")
    with sqlite3.connect(path) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid source DB")
        conn.execute("SELECT count(*) FROM alerts").fetchone()
    # Hash source before Store performs its idempotent legacy migration.
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    store = Store(path)
    try:
        value = snapshot(store)
    finally:
        store.close()
    endpoint = os.environ["STATE_ENDPOINT"]
    PrivateCheckpoint(endpoint, endpoint).request("initialize", snapshot=value,
                                                 source={"cache_key": key, "sha256": checksum})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Private migration failed:", type(exc).__name__)
        raise SystemExit(1)
