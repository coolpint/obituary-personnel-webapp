import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_alert_retries import ITEM, notifier
from yna_people_alert.main import run_once
from yna_people_alert.private_state import PrivateCheckpoint, restore, snapshot
from yna_people_alert.settings import Settings
from yna_people_alert.store import Store


class PrivateStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / 'source.db')
        self.net = patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP forbidden'))
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.store.close()
        self.temp.cleanup()

    def seed(self, status='sent', channel='slack'):
        item_id = self.store.upsert_item(ITEM)
        self.store.save_classification(item_id, '부고', [], 5, True, ['기자'], ['연합뉴스'])
        self.store.mark_alert(item_id, channel, status)
        return item_id

    def cycle(self, store, channels):
        dictionary = Mock()
        dictionary.refresh_if_needed.return_value = {'연합뉴스'}
        with patch('yna_people_alert.main.fetch_rss_items', return_value=[ITEM]):
            run_once(Settings(), store, dictionary, channels)

    def test_completed_history_has_no_article_text_url_guid_or_raw_xml(self):
        self.seed()
        value = snapshot(self.store)
        text = json.dumps(value)
        for private in [ITEM.guid, ITEM.title, ITEM.summary, ITEM.link, 'raw_xml']:
            self.assertNotIn(private, text)
        self.assertEqual(value['history'], [[hashlib.sha256(ITEM.guid.encode()).hexdigest(), 'slack', 'sent']])
        self.assertEqual(value['active'], [])

    def test_compact_history_still_prevents_existing_article_and_channel_backfill(self):
        self.seed()
        restored = restore(snapshot(self.store), self.path / 'restored.db')
        try:
            channels = [notifier('slack'), notifier('telegram')]
            self.cycle(restored, channels)
            self.assertTrue(restored.is_alerted(restored.upsert_item(ITEM), 'slack'))
            for channel in channels:
                channel.send.assert_not_called()
            self.assertEqual(snapshot(restored)['history'], snapshot(self.store)['history'])
        finally:
            restored.close()

    def test_pending_payload_roundtrip_and_successful_channel_not_replayed(self):
        self.seed('sent', 'slack')
        self.seed('failed', 'telegram')
        value = snapshot(self.store)
        self.assertNotIn('raw_xml', json.dumps(value))
        restored = restore(value, self.path / 'restored.db')
        try:
            channels = [notifier('slack'), notifier('telegram')]
            dictionary = Mock()
            dictionary.refresh_if_needed.return_value = {'연합뉴스'}
            with patch('yna_people_alert.main.fetch_rss_items', return_value=[]):
                run_once(Settings(), restored, dictionary, channels)
            channels[0].send.assert_not_called()
            channels[1].send.assert_called_once()
            after = snapshot(restored)
            self.assertEqual(after['active'], [])
            self.assertEqual(len(after['history']), 2)
        finally:
            restored.close()

    def test_uncertain_and_inflight_survive_private_restart_without_resend(self):
        self.seed('in_flight', 'slack')
        self.seed('uncertain', 'telegram')
        restored = restore(snapshot(self.store), self.path / 'restored.db')
        try:
            channels = [notifier('slack'), notifier('telegram')]
            self.cycle(restored, channels)
            for channel in channels:
                channel.send.assert_not_called()
            self.assertEqual(restored.delivery_problems(), 2)
        finally:
            restored.close()

    def test_legacy_exclusion_survives_without_article_payload(self):
        self.seed('failed')
        with self.store._conn:
            self.store._conn.execute("UPDATE alerts SET sent_at='2026-10-16 01:24:59'")
        self.store.close()
        self.store = Store(self.path / 'source.db')
        value = snapshot(self.store)
        self.assertEqual(value['history'][0][2], 'skipped_legacy')
        self.assertEqual(value['active'], [])

    def test_recent_slots_and_notifications_are_bounded_without_deleting_article_hashes(self):
        self.seed()
        with self.store._conn:
            self.store._conn.executemany("INSERT INTO collection_slots(slot,status,run_id) VALUES (?,'succeeded','1')", [(f'{i:03}',) for i in range(200)])
            self.store._conn.executemany("INSERT INTO system_notifications VALUES (?,'sent')", [(f'{i:03}',) for i in range(200)])
        value = snapshot(self.store)
        self.assertEqual(len(value['slots']), 60)
        self.assertEqual(len(value['notifications']), 120)
        self.assertEqual(len(value['history']), 1)

    def test_one_mib_size_cap_fails_instead_of_evicting_dedup_history(self):
        self.seed('pending')
        with self.store._conn:
            self.store._conn.execute('UPDATE items SET summary=?', ('x' * 1024 * 1024,))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            snapshot(self.store)

    def test_invalid_or_existing_local_database_cannot_be_silently_replaced(self):
        with self.assertRaises(ValueError):
            restore({}, self.path / 'bad.db')
        with self.assertRaises(ValueError):
            restore(snapshot(self.store), self.path / 'source.db')

    def test_actual_runner_roundtrip_uses_private_checkpoint_and_skips_duplicate_slot(self):
        from yna_people_alert.scheduled import run_slot
        memory = {'revision': 1, 'snapshot': snapshot(self.store)}
        session = Mock()
        def post(url, **kwargs):
            operation = kwargs['json']
            if operation['action'] == 'checkpoint':
                self.assertEqual(operation['revision'], memory['revision'])
                memory['snapshot'] = operation['snapshot']
                memory['revision'] += 1
                result = {'revision': memory['revision']}
            elif operation['action'] == 'acquire':
                result = memory.copy()
            else:
                result = {'released': True}
            return Mock(status_code=200, json=Mock(return_value=result))
        session.post.side_effect = post
        endpoint = 'https://yna.example.workers.dev/state'
        channel = notifier('slack')
        dictionary = Mock()
        dictionary.refresh_if_needed.return_value = {'연합뉴스'}
        with patch('yna_people_alert.main.fetch_rss_items', return_value=[ITEM]) as fetch:
            for attempt in range(2):
                client = PrivateCheckpoint(endpoint, endpoint, session)
                client.token = Mock(return_value='fake-signed-oidc')
                store = client.open(self.path / f'run{attempt}.db')
                try:
                    ran = run_slot('2026-10-19T10:07+09:00', str(attempt), Settings(), store, dictionary, [channel])
                    self.assertEqual(ran, attempt == 0)
                finally:
                    store.close()
                    client.release()
            channel.send.assert_called_once()
            fetch.assert_called_once()
        self.assertEqual(memory['snapshot']['active'], [])
        self.assertNotIn(ITEM.title, json.dumps(memory))


class PrivateMigrationTests(unittest.TestCase):
    def test_exact_cache_required_before_any_private_request(self):
        from yna_people_alert.bootstrap_private import main
        with patch.dict(os.environ, {'APPROVED_CACHE_KEY': 'reviewed', 'MATCHED_CACHE_KEY': 'wrong'}), \
             patch('yna_people_alert.bootstrap_private.PrivateCheckpoint') as client:
            with self.assertRaises(ValueError):
                main()
            client.assert_not_called()

    def test_migration_calls_initialize_only_with_minimal_snapshot(self):
        from yna_people_alert.bootstrap_private import main
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            store = Store(path)
            item_id = store.upsert_item(ITEM)
            store.mark_alert(item_id, 'slack', 'sent')
            store.close()
            with patch.dict(os.environ, {'APPROVED_CACHE_KEY': 'yna-people-alert-data-123',
                                         'MATCHED_CACHE_KEY': 'yna-people-alert-data-123',
                                         'STATE_ENDPOINT': 'https://yna.example.workers.dev/state'}), \
                 patch('yna_people_alert.bootstrap_private.Path', return_value=path), \
                 patch('yna_people_alert.bootstrap_private.PrivateCheckpoint') as client:
                main()
                call = client.return_value.request.call_args
                self.assertEqual(call.args, ('initialize',))
                self.assertNotIn(ITEM.title, json.dumps(call.kwargs['snapshot']))
                self.assertEqual(call.kwargs['source']['cache_key'], 'yna-people-alert-data-123')
                self.assertEqual(len(call.kwargs['source']['sha256']), 64)


class PrivateClientTests(unittest.TestCase):
    endpoint = 'https://yna-test.example.workers.dev/state'

    def client(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=Mock(return_value={'value': 'fake-oidc-never-log'}))
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={'revision': 2}))
        return PrivateCheckpoint(self.endpoint, self.endpoint, session), session

    @patch.dict(os.environ, {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://example.actions.githubusercontent.com/token?api-version=2.0',
                             'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'fake-ephemeral-request-token'})
    def test_oidc_token_is_ephemeral_audience_bound_and_only_in_auth_header(self):
        client, session = self.client()
        client.request('release')
        self.assertIn('audience=https%3A%2F%2Fyna-test.example.workers.dev%2Fstate', session.get.call_args.args[0])
        self.assertEqual(session.post.call_args.kwargs['headers']['Authorization'], 'Bearer fake-oidc-never-log')
        self.assertNotIn('fake-', json.dumps(session.post.call_args.kwargs['json']))
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])

    def test_no_shared_credential_or_non_https_unapproved_endpoint(self):
        for url in ['http://example.workers.dev/state', 'https://evil.invalid/state', 'https://example.workers.dev/state?token=x']:
            with self.assertRaises(ValueError):
                PrivateCheckpoint(url, url)
        with self.assertRaises(ValueError):
            PrivateCheckpoint(self.endpoint, 'different-audience')

    def test_http_errors_do_not_expose_body_or_token(self):
        client, session = self.client()
        client.token = Mock(return_value='fake-token')
        session.post.side_effect = RuntimeError('https://secret-token-and-payload')
        with self.assertRaisesRegex(RuntimeError, '^Private state request failed$'):
            client.request('acquire')

    def test_lost_response_does_not_advance_client_revision(self):
        client, session = self.client()
        client.revision = 1
        client.token = Mock(return_value='fake-token')
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.db')
            client.store = store
            session.post.side_effect = RuntimeError('timeout')
            try:
                with self.assertRaises(RuntimeError):
                    client()
                self.assertEqual(client.revision, 1)
            finally:
                store.close()
