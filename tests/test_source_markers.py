import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yna_people_alert.classifier import classify_item
from yna_people_alert.main import run_once
from yna_people_alert.rss_fetcher import RSSItem
from yna_people_alert.settings import Settings
from yna_people_alert.store import Store
from yna_people_alert.text_utils import clean_rss_text, strip_source_markers


# Public RSS descriptions captured on 2026-11-09. The source marker really is
# truncated in the feed, rather than by the alert renderer.
RESEARCH_TITLE = '[인사] 한국전기연구원'
RESEARCH_SUMMARY = (
    '▲ 시험부원장 이동준 ▲ 전략정책본부장 김슬기 ▲ 스마트그리드연구본부장 전진홍 '
    '▲ 기획조정본부장 문소민 ▲ 경영지원본부장 고성주 (창원=연합뉴스...'
)
JOURNALIST_TITLE = '[부고] 김병수(중앙일보 리더)씨 부친상'
JOURNALIST_SUMMARY = (
    '▲ 김종현(향년 73세)씨 별세, 김병수(중앙일보 비즈솔루션본부 마케팅솔루션국 리더)씨 '
    '부친상 = 8일 오후 5시 42분, 대전 나진장례식장 특...'
)
OUTLETS = {'연합뉴스', '연합뉴스TV', '중앙일보', '뉴시스'}


class SourceMarkerTests(unittest.TestCase):
    def test_removes_complete_source_markers(self):
        self.assertEqual(strip_source_markers('인사 내용 (창원=연합뉴스) (끝)'), '인사 내용')

    def test_removes_truncated_source_markers_at_line_end(self):
        for marker in (
            '(창원=연합뉴스...', '(서울=연합뉴스…', '(도쿄=연합뉴스TV',
            '(부산=뉴시스...', '(서울 = 연합뉴스 ...   ',
        ):
            with self.subTest(marker=marker):
                self.assertEqual(strip_source_markers('인사 내용 ' + marker), '인사 내용')

    def test_does_not_remove_next_line_or_real_affiliation(self):
        text = '인사 내용 (창원=연합뉴스...\n김민수(중앙일보 리더)씨 부친상'
        self.assertEqual(strip_source_markers(text), '인사 내용 김민수(중앙일보 리더)씨 부친상')

    def test_preserves_complete_and_truncated_affiliations_without_equals(self):
        for text in ('김민수(연합뉴스 기자)씨 부친상', '김민수(연합뉴스 기자...', '김민수(중앙일보 리더)'):
            with self.subTest(text=text):
                self.assertEqual(strip_source_markers(text), text)

    def test_alert_summary_does_not_display_truncated_source(self):
        self.assertEqual(clean_rss_text(RESEARCH_SUMMARY), RESEARCH_SUMMARY.split(' (창원=')[0])


class ClassifierSourceMarkerTests(unittest.TestCase):
    def classify(self, title, summary):
        return classify_item(title + '\n' + summary, OUTLETS, threshold=5)

    def test_research_institute_is_not_media_related(self):
        result = self.classify(RESEARCH_TITLE, RESEARCH_SUMMARY)
        self.assertEqual(result.category, '인사')
        self.assertFalse(result.is_media_related)
        self.assertEqual(result.matched_outlets, [])
        self.assertEqual(result.matched_roles, [])
        self.assertEqual(result.media_score, 0)

    def test_research_institute_with_complete_source_is_not_media_related(self):
        result = self.classify(RESEARCH_TITLE, '▲ 원장 김민수 (창원=연합뉴스)')
        self.assertFalse(result.is_media_related)

    def test_source_roles_are_not_subject_roles(self):
        for marker in ('(서울=연합뉴스 기자 특파원...', '(서울=연합뉴스 기자 특파원)'):
            with self.subTest(marker=marker):
                result = self.classify(RESEARCH_TITLE, '▲ 원장 김민수 ' + marker)
                self.assertFalse(result.is_media_related)
                self.assertEqual(result.matched_outlets, [])
                self.assertEqual(result.matched_roles, [])

    def test_actual_joongang_affiliation_without_standard_role_is_preserved(self):
        result = self.classify(JOURNALIST_TITLE, JOURNALIST_SUMMARY)
        self.assertEqual(result.category, '부고')
        self.assertTrue(result.is_media_related)
        self.assertEqual(result.matched_outlets, ['중앙일보'])

    def test_actual_yonhap_personnel_and_obituary_remain_targets(self):
        for title, summary in (
            ('[인사] 연합뉴스', '▲ 경제부장 김민수 (서울=연합뉴스...'),
            ('[부고] 김민수(연합뉴스 기자)씨 부친상', '▲ 김철수씨 별세 (서울=연합뉴스...'),
        ):
            with self.subTest(title=title):
                result = self.classify(title, summary)
                self.assertTrue(result.is_media_related)
                self.assertIn('연합뉴스', result.matched_outlets)


class SourceMarkerDeliveryTests(unittest.TestCase):
    def test_false_positive_not_queued_and_true_target_sent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / 'state.db')
            store = Store(settings.db_path)
            try:
                dictionary = Mock()
                dictionary.refresh_if_needed.return_value = OUTLETS
                slack = Mock(channel_name='slack')
                items = [
                    RSSItem('research', RESEARCH_TITLE, RESEARCH_SUMMARY, 'https://example.invalid/research', '', ''),
                    RSSItem('journalist', JOURNALIST_TITLE, JOURNALIST_SUMMARY, 'https://example.invalid/journalist', '', ''),
                ]
                with patch('yna_people_alert.main.fetch_rss_items', return_value=items), \
                     patch('requests.sessions.Session.request', side_effect=AssertionError('real HTTP forbidden')):
                    run_once(settings, store, dictionary, [slack])
                    run_once(settings, store, dictionary, [slack])
                slack.send.assert_called_once()
                self.assertIn(JOURNALIST_TITLE.removeprefix('[부고] '), slack.send.call_args.args[0])
                rows = store._conn.execute('SELECT channel, status FROM alerts').fetchall()
                self.assertEqual([tuple(row) for row in rows], [('slack', 'sent')])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
