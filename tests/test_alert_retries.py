import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yna_people_alert.main import run_once
from yna_people_alert.rss_fetcher import RSSItem
from yna_people_alert.settings import Settings
from yna_people_alert.store import RETRY_START_UTC, Store


ITEM = RSSItem('test-guid', '[부고] 연합뉴스 기자 별세', '모의 기사',
               'https://example.invalid/article', '', '<item/>')


def notifier(channel, fail=False):
    result = Mock(channel_name=channel)
    if fail:
        result.send.side_effect = RuntimeError('sensitive-url-must-not-be-logged')
    return result


class AlertRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.settings = Settings(db_path=self.path)
        self.dictionary = Mock()
        self.dictionary.refresh_if_needed.return_value = {'연합뉴스'}
        # A guard makes any accidental real HTTP request fail the test.
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('real HTTP forbidden'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.store.close()
        self.temp.cleanup()

    def cycle(self, notifiers, items=None, fetch_error=None):
        with patch('yna_people_alert.main.fetch_rss_items', return_value=[ITEM] if items is None else items,
                   side_effect=fetch_error):
            run_once(self.settings, self.store, self.dictionary, notifiers)

    def statuses(self):
        with sqlite3.connect(self.path) as conn:
            return dict(conn.execute('SELECT channel, status FROM alerts'))

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def seed_failure(self, timestamp, partial=False):
        channels = [notifier('slack', fail=not partial), notifier('telegram', fail=True)]
        self.cycle(channels)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE alerts SET sent_at = ?', (timestamp,))
        self.reopen()

    def test_success_and_duplicate_prevention(self):
        channels = [notifier('slack'), notifier('telegram')]
        self.cycle(channels)
        self.cycle(channels)
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'sent'})
        for channel in channels:
            channel.send.assert_called_once()
        self.assertTrue(self.store.is_alerted(1, 'slack'))

    def test_partial_failure_retries_only_failed_channel_after_restart_without_rss(self):
        slack, telegram = notifier('slack'), notifier('telegram', fail=True)
        self.cycle([slack, telegram])
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'failed'})
        self.assertFalse(self.store.is_alerted(1, 'telegram'))
        self.reopen()
        telegram.send.side_effect = None
        self.cycle([slack, telegram], items=[])
        self.cycle([slack, telegram], items=[])
        slack.send.assert_called_once()
        self.assertEqual(telegram.send.call_count, 2)
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'sent'})

    def test_total_failure_repeated_failure_then_success(self):
        channels = [notifier('slack', fail=True), notifier('telegram', fail=True)]
        self.cycle(channels)
        self.cycle(channels, items=[])
        self.assertEqual(self.statuses(), {'slack': 'failed', 'telegram': 'failed'})
        for channel in channels:
            channel.send.side_effect = None
        self.cycle(channels, items=[])
        self.cycle(channels)
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'sent'})
        for channel in channels:
            self.assertEqual(channel.send.call_count, 3)

    def test_preapproval_failures_excluded_and_migration_idempotent(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                self.store._conn.execute('DELETE FROM alerts')
                self.store._conn.commit()
                self.seed_failure('2026-10-16 01:24:59', partial=partial)
                self.reopen()
                channels = [notifier('slack'), notifier('telegram')]
                self.cycle(channels)
                self.cycle(channels, items=[])
                for channel in channels:
                    channel.send.assert_not_called()
                self.assertEqual(self.statuses(), {
                    'slack': 'sent' if partial else 'skipped_legacy', 'telegram': 'skipped_legacy'})

    def test_approval_boundary_failure_remains_retryable(self):
        self.seed_failure(RETRY_START_UTC)
        channels = [notifier('slack'), notifier('telegram')]
        self.cycle(channels, items=[])
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'sent'})
        for channel in channels:
            channel.send.assert_called_once()

    def test_new_channel_does_not_backfill_existing_item(self):
        slack, telegram = notifier('slack'), notifier('telegram')
        self.cycle([slack])
        self.cycle([slack, telegram])
        slack.send.assert_called_once()
        telegram.send.assert_not_called()
        self.assertEqual(self.statuses(), {'slack': 'sent'})

    def test_all_channels_queued_before_first_send_and_survive_interruption(self):
        slack, telegram = notifier('slack'), notifier('telegram')
        slack.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.cycle([slack, telegram])
        self.assertEqual(self.statuses(), {'slack': 'pending', 'telegram': 'pending'})
        self.reopen()
        slack.send.side_effect = None
        self.cycle([slack, telegram], items=[])
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'sent'})
        telegram.send.assert_called_once()

    def test_pending_delivered_even_when_feed_fetch_fails(self):
        slack = notifier('slack', fail=True)
        self.cycle([slack])
        slack.send.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'feed unavailable'):
            self.cycle([slack], fetch_error=RuntimeError('feed unavailable'))
        self.assertEqual(self.statuses(), {'slack': 'sent'})

    def test_failure_does_not_log_or_store_secret_exception_text(self):
        with self.assertLogs('yna_people_alert', level='ERROR') as logs:
            self.cycle([notifier('telegram', fail=True)])
        self.assertNotIn('sensitive-url', '\n'.join(logs.output))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT error FROM alerts').fetchone()[0], 'RuntimeError')

    def test_unconfigured_failed_channel_is_preserved_until_restored(self):
        slack, telegram = notifier('slack'), notifier('telegram', fail=True)
        self.cycle([slack, telegram])
        self.cycle([slack], items=[])
        self.assertEqual(self.statuses(), {'slack': 'sent', 'telegram': 'failed'})
        telegram.send.side_effect = None
        self.cycle([slack, telegram], items=[])
        telegram.send.assert_called()
        self.assertEqual(telegram.send.call_count, 2)
        slack.send.assert_called_once()

    def test_legacy_exclusion_does_not_block_a_new_item(self):
        self.seed_failure('2026-10-16 01:24:59')
        channels = [notifier('slack'), notifier('telegram')]
        new_item = RSSItem('new-guid', ITEM.title, ITEM.summary, ITEM.link, '', '<item/>')
        self.cycle(channels, items=[ITEM, new_item])
        for channel in channels:
            channel.send.assert_called_once()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE status = 'skipped_legacy'"
            ).fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE status = 'sent'"
            ).fetchone()[0], 2)

    def test_non_media_item_is_not_queued(self):
        channels = [notifier('slack'), notifier('telegram')]
        self.cycle(channels, items=[RSSItem('other', '일반 뉴스', '', '', '', '<item/>')])
        self.assertEqual(self.statuses(), {})
        for channel in channels:
            channel.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
