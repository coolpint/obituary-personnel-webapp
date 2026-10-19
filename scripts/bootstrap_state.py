"""Explicit, non-collecting migration. Run only via the approved manual workflow."""
from pathlib import Path
import json
import hashlib
import os
import sqlite3
import subprocess


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Migration Git operation failed")
    return result.stdout.strip()


def main():
    requested = os.environ.get("APPROVED_CACHE_KEY", "")
    if not requested or requested != os.environ.get("MATCHED_CACHE_KEY"):
        raise ValueError("Exact reviewed cache key required")
    db = Path("data/yna_people_alert.db")
    if not db.is_file():
        raise ValueError("Approved cache DB is missing; refusing empty history")
    with sqlite3.connect(db) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid source DB")
        conn.execute("SELECT count(*) FROM alerts").fetchone()
    if git("ls-remote", "--heads", "origin", "refs/heads/alert-state"):
        raise ValueError("State branch already exists; do not overwrite")
    # The existing checkout's short-lived authentication remains managed by
    # actions/checkout. No token or remote credential is read by this script.
    git("switch", "--orphan", "alert-state")
    Path("health.json").write_text(json.dumps({
        "slots": [], "migration": "cache-seeded", "source_cache_key": requested,
        "source_db_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
    }) + "\n")
    git("add", "-f", "--", "data/yna_people_alert.db", "health.json")
    if Path("data/outlets_cache.json").is_file():
        git("add", "-f", "--", "data/outlets_cache.json")
    git("-c", "user.name=YNA state", "-c", "user.email=yna-state@users.noreply.github.com",
        "commit", "-m", "Seed existing delivery history [skip ci]")
    git("push", "origin", "HEAD:refs/heads/alert-state")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not expose network exception output / credentials in logs.
        print("State migration failed:", type(exc).__name__)
        raise SystemExit(1)
