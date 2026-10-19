import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_alert_retries import ITEM, notifier
from yna_people_alert.scheduled import exclusive_lock, resolve_slot, run_slot
from yna_people_alert.settings import Settings
from yna_people_alert.store import Store

AM = "2026-10-19T10:07+09:00"
PM = "2026-10-19T18:07+09:00"


class SlotTests(unittest.TestCase):
    def test_external_and_delayed_schedule_share_slot(self):
        self.assertEqual(resolve_slot({"inputs": {"slot": AM}}, "2026-10-19T01:07:01Z"), AM)
        self.assertEqual(resolve_slot({"schedule": "7 1 * * *"}, "2026-10-19T05:55:00Z"), AM)
        self.assertEqual(resolve_slot({"schedule": "7 9 * * *"}, "2026-10-19T13:55:00Z"), PM)

    def test_kst_date_boundary_uses_due_date(self):
        slot = "2026-10-19T18:07+09:00"
        self.assertEqual(resolve_slot({"inputs": {"slot": slot}}, "2026-10-19T15:30:00Z"), slot)

    def test_invalid_future_old_and_ambiguous_slots_fail_closed(self):
        for event, created in [
            ({"inputs": {}}, "2026-10-19T01:07:00Z"),
            ({"inputs": {"slot": "2026-10-19T01:07+00:00"}}, "2026-10-19T01:07:00Z"),
            ({"inputs": {"slot": "2026-10-19T11:07+09:00"}}, "2026-10-19T05:07:00Z"),
            ({"inputs": {"slot": PM}}, "2026-10-19T01:07:00Z"),
            ({"inputs": {"slot": AM}}, "2026-10-21T01:07:00Z"),
            ({"schedule": "7 1 * * *"}, "2026-10-19T08:07:00Z"),
            ({"schedule": "bad"}, "2026-10-19T01:07:00Z"),
        ]:
            with self.subTest(event=event), self.assertRaises(ValueError):
                resolve_slot(event, created)


class DurableRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.saved = Path(self.temp.name) / "remote.db"
        self.store = Store(self.path, checkpoint=self.checkpoint)
        self.dictionary = Mock()
        self.dictionary.refresh_if_needed.return_value = {"연합뉴스"}
        self.settings = Settings(db_path=self.path)
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Real HTTP forbidden"))
        self.network.start()
        self.feed = patch("yna_people_alert.main.fetch_rss_items", return_value=[ITEM])
        self.fetch = self.feed.start()

    def tearDown(self):
        self.feed.stop()
        self.network.stop()
        self.store.close()
        self.temp.cleanup()

    def checkpoint(self):
        shutil.copyfile(self.path, self.saved)

    def restore(self):
        self.store.close()
        shutil.copyfile(self.saved, self.path)
        self.store = Store(self.path, checkpoint=self.checkpoint)

    def run_slot(self, slot=AM, channels=None):
        return run_slot(slot, "123", self.settings, self.store, self.dictionary,
                        channels if channels is not None else [notifier("slack")])

    def test_delayed_schedule_and_retry_do_not_collect_or_send_again(self):
        slack = notifier("slack")
        self.assertTrue(self.run_slot(channels=[slack]))
        self.restore()
        self.assertFalse(self.run_slot(channels=[slack]))
        slack.send.assert_called_once()
        self.fetch.assert_called_once()

    def test_next_slot_collects_without_replaying_existing_article(self):
        slack = notifier("slack")
        self.run_slot(channels=[slack])
        self.restore()
        self.run_slot(PM, [slack])
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(sum("[부고" in call.args[0] for call in slack.send.call_args_list), 1)

    def test_checkpoint_intent_precedes_send(self):
        slack = notifier("slack")
        def sent(message):
            with sqlite3.connect(self.saved) as conn:
                self.assertEqual(conn.execute("SELECT status FROM alerts").fetchone()[0], "in_flight")
        slack.send.side_effect = sent
        self.run_slot(channels=[slack])

    def test_failed_checkpoint_prevents_send(self):
        self.store.checkpoint = Mock(side_effect=RuntimeError("storage unavailable"))
        slack = notifier("slack")
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack])
        slack.send.assert_not_called()

    def test_checkpoint_failure_after_claim_before_send_prevents_delivery(self):
        self.store.checkpoint = Mock(side_effect=[None, RuntimeError("storage unavailable"), None])
        slack = notifier("slack")
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack])
        slack.send.assert_not_called()

    def test_crash_after_sink_accepts_never_replays_uncertain_article(self):
        slack, telegram = notifier("slack"), notifier("telegram")
        slack.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_slot(channels=[slack, telegram])
        self.restore()
        slack.send.side_effect = None
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack, telegram])
        slack.send.assert_called_once()
        telegram.send.assert_called_once()
        self.assertEqual(self.store.delivery_problems(), 1)

    def test_partial_timeout_does_not_resend_success_or_uncertain_channel(self):
        slack, telegram = notifier("slack"), notifier("telegram", fail=True)
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack, telegram])
        self.restore()
        telegram.send.side_effect = None
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack, telegram])
        slack.send.assert_called_once()
        telegram.send.assert_called_once()

    def test_feed_failure_can_resume_same_slot(self):
        self.fetch.side_effect = RuntimeError("feed outage")
        with self.assertRaises(RuntimeError):
            self.run_slot()
        self.restore()
        self.fetch.side_effect = None
        self.assertTrue(self.run_slot())

    def test_existing_failed_delivery_retries_without_historical_replay(self):
        item_id = self.store.upsert_item(ITEM)
        self.store.save_classification(item_id, "부고", [], 5, True, [], [])
        self.store.mark_alert(item_id, "slack", "failed")
        slack = notifier("slack")
        self.run_slot(channels=[slack])
        slack.send.assert_called_once()

    def test_evening_heartbeat_is_once_even_if_run_retries(self):
        slack = notifier("slack")
        self.run_slot(PM, [slack])
        self.restore()
        self.run_slot(PM, [slack])
        self.assertEqual(slack.send.call_count, 2)  # one article, one heartbeat

    def test_second_process_cannot_lock_same_state(self):
        lock = self.path.with_suffix(".lock")
        with exclusive_lock(lock):
            with self.assertRaises(BlockingIOError), exclusive_lock(lock):
                self.fail("Second lock admitted")

    def test_uncertain_heartbeat_never_repeats_or_becomes_healthy_on_retry(self):
        slack = notifier("slack")
        def send(message):
            if "heartbeat" in message:
                raise RuntimeError("response lost")
        slack.send.side_effect = send
        with self.assertRaises(RuntimeError):
            self.run_slot(PM, [slack])
        self.restore()
        slack.send.side_effect = None
        with self.assertRaises(RuntimeError):
            self.run_slot(PM, [slack])
        self.assertEqual(slack.send.call_count, 2)

    def test_sink_success_then_receipt_push_failure_never_replays(self):
        slack = notifier("slack")
        def fail_after_send():
            with sqlite3.connect(self.path) as conn:
                status = conn.execute("SELECT status FROM alerts").fetchone()
            if status and status[0] == "sent":
                raise RuntimeError("receipt push failed")
            self.checkpoint()
        self.store.checkpoint = fail_after_send
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack])
        self.restore()
        with self.assertRaises(RuntimeError):
            self.run_slot(channels=[slack])
        slack.send.assert_called_once()
