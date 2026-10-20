import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_alert_retries import ITEM
from yna_people_alert.main import build_notifiers, run_once
from yna_people_alert.settings import Settings
from yna_people_alert.store import Store


class SlackOnlyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.dictionary = Mock()
        self.dictionary.refresh_if_needed.return_value = {'연합뉴스'}
        # Match the production workflow's forwarded environment, using fake data.
        with patch.dict(os.environ, {'SLACK_WEBHOOK_URL': 'https://example.invalid/slack'}, clear=True), \
             patch('yna_people_alert.main.TelegramNotifier') as telegram:
            self.settings = Settings.from_env()
            self.channels = build_notifiers(self.settings)
            telegram.assert_not_called()
        self.assertEqual([n.channel_name for n in self.channels], ['slack'])
        self.slack_send = Mock()
        self.channels[0].send = self.slack_send
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP forbidden'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.store.close()
        self.temp.cleanup()

    def cycle(self):
        with patch('yna_people_alert.main.fetch_rss_items', return_value=[ITEM]):
            run_once(self.settings, self.store, self.dictionary, self.channels)

    def test_workflow_forwards_slack_but_no_telegram_credentials(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/yna-people-alert.yml').read_text()
        self.assertIn('SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}', workflow)
        self.assertNotIn('TELEGRAM_BOT_TOKEN:', workflow)
        self.assertNotIn('TELEGRAM_CHAT_ID:', workflow)
        self.assertIn('group: yna-people-alert', workflow)
        self.assertIn('actions/cache/save@v4', workflow)

    def test_new_media_article_sent_once_to_slack_with_no_telegram_queue(self):
        self.cycle()
        self.cycle()
        self.slack_send.assert_called_once()
        self.assertIn('[부고][언론인]', self.slack_send.call_args.args[0])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT channel,status FROM alerts').fetchall(), [('slack', 'sent')])

    def test_slack_failure_retries_after_restart_without_touching_telegram_history(self):
        self.slack_send.side_effect = RuntimeError('simulated-failure')
        self.cycle()
        self.store.mark_alert(1, 'telegram', 'failed', 'ConnectionError')
        # Give the record a deterministic post-approval timestamp.
        with self.store._conn:
            self.store._conn.execute("UPDATE alerts SET sent_at='2026-10-20 05:14:25' WHERE channel='telegram'")
        before = tuple(self.store._conn.execute("SELECT * FROM alerts WHERE channel='telegram'").fetchone())
        self.store.close()
        self.store = Store(self.path)
        self.slack_send.side_effect = None
        self.cycle()
        self.cycle()
        self.assertEqual(self.slack_send.call_count, 2)  # one failure, one success
        after = tuple(self.store._conn.execute("SELECT * FROM alerts WHERE channel='telegram'").fetchone())
        self.assertEqual(after, before)
        self.assertTrue(self.store.is_alerted(1, 'slack'))

    def test_pending_telegram_is_preserved_without_being_sent(self):
        self.cycle()
        self.store.mark_alert(1, 'telegram', 'pending')
        before = tuple(self.store._conn.execute("SELECT * FROM alerts WHERE channel='telegram'").fetchone())
        self.cycle()
        self.slack_send.assert_called_once()
        after = tuple(self.store._conn.execute("SELECT * FROM alerts WHERE channel='telegram'").fetchone())
        self.assertEqual(after, before)
