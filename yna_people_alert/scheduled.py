"""Durable, serialized entry point used by *all* Actions collection triggers."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path

from .main import build_notifiers, run_once
from .outlet_dictionary import OutletDictionary
from .settings import Settings
from .store import Store
from .private_state import PrivateCheckpoint

KST = timezone(timedelta(hours=9))
UTC = timezone.utc


def resolve_slot(event: dict, created_at: str) -> str:
    """Use run creation, never job start / retry time, to infer legacy cron slots."""
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(KST)
    if event.get("schedule"):
        hour = {"7 1 * * *": 10, "7 9 * * *": 18}.get(event["schedule"])
        if hour is None:
            raise ValueError("Unknown schedule")
        due = created.replace(hour=hour, minute=7, second=0, microsecond=0)
        if due > created:
            due -= timedelta(days=1)
        # GitHub supplies no original scheduled date. Do not guess after a day
        # boundary / extreme delay; explicit external slot IDs avoid ambiguity.
        if created - due > timedelta(hours=6):
            raise ValueError("Legacy schedule date is ambiguous")
    else:
        value = event.get("inputs", {}).get("slot", "")
        due = datetime.strptime(value, "%Y-%m-%dT%H:%M%z").astimezone(KST)
        if value != due.isoformat(timespec="minutes") or due.hour not in (10, 18) or due.minute != 7:
            raise ValueError("Expected canonical KST collection slot")
        if not timedelta(0) <= created - due <= timedelta(hours=36):
            raise ValueError("Slot is future or too old")
    return due.isoformat(timespec="minutes")


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def evening_heartbeat(store: Store, slot: str, notifiers, run_url: str) -> None:
    if "T18:07" not in slot:
        return
    slack = next((n for n in notifiers if n.channel_name == "slack"), None)
    if slack is None:
        return
    key = "heartbeat:" + slot
    with store._conn:
        inserted = store._conn.execute(
            "INSERT OR IGNORE INTO system_notifications VALUES (?, 'in_flight')", (key,)
        ).rowcount
    if not inserted:
        status = store._conn.execute(
            "SELECT status FROM system_notifications WHERE name=?", (key,)
        ).fetchone()[0]
        if status != "sent":
            raise RuntimeError("Heartbeat delivery requires reconciliation")
        return
    store.flush()
    slack.send(f"[정상] YNA People Alert heartbeat\n회차: {slot}\n수집·전송 정상 완료\n로그: {run_url}")
    with store._conn:
        store._conn.execute("UPDATE system_notifications SET status='sent' WHERE name=?", (key,))
    store.flush()


def run_slot(slot: str, run_id: str, settings: Settings, store: Store, dictionary, notifiers,
             run_url: str = "") -> bool:
    if not any(n.channel_name != "console" for n in notifiers):
        raise RuntimeError("Durable collection requires configured delivery channels")
    if not store.claim_slot(slot, run_id):
        logging.info("Already completed slot=%s", slot)
        return False
    try:
        run_once(settings, store, dictionary, notifiers)
        if store.delivery_problems():
            raise RuntimeError("Pending or uncertain deliveries require reconciliation")
        evening_heartbeat(store, slot, notifiers, run_url)
    except BaseException as exc:
        store.finish_slot(slot, "failed", type(exc).__name__)
        raise
    store.finish_slot(slot, "succeeded")
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        slot = resolve_slot(event, os.environ["RUN_CREATED_AT"])
        settings = Settings.from_env()
        # Must acquire before opening SQLite. Every local DB user in production
        # must use this entry point; remote isolation is Actions concurrency.
        with exclusive_lock(settings.db_path.with_suffix(".lock")):
            checkpoint = PrivateCheckpoint(os.environ["STATE_ENDPOINT"], os.environ["STATE_ENDPOINT"])
            store = checkpoint.open(settings.db_path)
            try:
                dictionary = OutletDictionary(
                    settings.outlet_cache_path, settings.office_list_url, settings.daum_cplist_url,
                    settings.outlet_refresh_hours, settings.request_timeout_seconds,
                )
                run_slot(slot, os.environ["GITHUB_RUN_ID"], settings, store, dictionary,
                         build_notifiers(settings), os.environ.get("RUN_URL", ""))
            finally:
                store.close()
                checkpoint.release()
    except Exception as exc:
        logging.error("Scheduled collection failed: %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
