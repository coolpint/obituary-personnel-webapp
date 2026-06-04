import unittest
from unittest.mock import patch

import requests

from yna_people_alert.rss_fetcher import fetch_rss_items


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>인사</title>
      <description>테스트 요약</description>
      <link>https://example.com/a</link>
      <guid>guid-1</guid>
      <pubDate>Sun, 24 May 2026 00:00:00 +0900</pubDate>
    </item>
  </channel>
</rss>
"""


class FakeResponse:
    text = RSS_XML

    def raise_for_status(self):
        return None


class FetchRssItemsTests(unittest.TestCase):
    def test_retries_transient_connection_reset_then_returns_items(self):
        attempts = []

        def fake_get(url, **kwargs):
            attempts.append((url, kwargs))
            if len(attempts) < 3:
                raise requests.exceptions.ConnectionError("connection reset by peer")
            return FakeResponse()

        with patch("yna_people_alert.rss_fetcher.requests.get", side_effect=fake_get), patch("time.sleep"):
            items = fetch_rss_items("https://www.yna.co.kr/rss/people.xml", timeout_seconds=15)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].guid, "guid-1")
        self.assertEqual(len(attempts), 3)

    def test_sends_user_agent_header_to_reduce_server_resets(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

        with patch("yna_people_alert.rss_fetcher.requests.get", side_effect=fake_get):
            fetch_rss_items("https://www.yna.co.kr/rss/people.xml", timeout_seconds=15)

        headers = calls[0].get("headers", {})
        self.assertIn("User-Agent", headers)
        self.assertIn("YNA-People-Alert", headers["User-Agent"])

    def test_repairs_unescaped_ampersand_in_media_url(self):
        class BadXmlResponse:
            text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <item>
      <title>부고</title>
      <description>테스트 요약</description>
      <link>https://example.com/a</link>
      <guid>guid-1</guid>
      <pubDate>Sun, 24 May 2026 00:00:00 +0900</pubDate>
      <media:content url="https://www.youtube.com/embed/CDr741dcxKY?si=abc&start=126" medium="video" />
    </item>
  </channel>
</rss>
"""

            def raise_for_status(self):
                return None

        with patch("yna_people_alert.rss_fetcher.requests.get", return_value=BadXmlResponse()):
            items = fetch_rss_items("https://www.yna.co.kr/rss/people.xml", timeout_seconds=15)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].guid, "guid-1")


if __name__ == "__main__":
    unittest.main()
