from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    rss_url: str = "https://www.yna.co.kr/rss/people.xml"
    office_list_url: str = "https://news.naver.com/main/officeList.naver"
    daum_cplist_url: str = "https://news.daum.net/cplist"
    poll_seconds: int = 43200
    media_score_threshold: int = 5
    request_timeout_seconds: int = 15
    outlet_refresh_hours: int = 12
    db_path: Path = Path("./data/yna_people_alert.db")
    outlet_cache_path: Path = Path("./data/outlets_cache.json")
    slack_webhook_url: str | None = None
    slack_mention: str | None = None
    include_summary_in_alert: bool = True
    alert_summary_max_chars: int = 500
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            rss_url=os.getenv("RSS_URL", "https://www.yna.co.kr/rss/people.xml"),
            office_list_url=os.getenv(
                "OFFICE_LIST_URL",
                "https://news.naver.com/main/officeList.naver",
            ),
            daum_cplist_url=os.getenv(
                "DAUM_CPLIST_URL",
                "https://news.daum.net/cplist",
            ),
            poll_seconds=int(os.getenv("POLL_SECONDS", "43200")),
            media_score_threshold=int(os.getenv("MEDIA_SCORE_THRESHOLD", "5")),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15")),
            outlet_refresh_hours=int(os.getenv("OUTLET_REFRESH_HOURS", "12")),
            db_path=Path(os.getenv("DB_PATH", "./data/yna_people_alert.db")),
            outlet_cache_path=Path(
                os.getenv("OUTLET_CACHE_PATH", "./data/outlets_cache.json")
            ),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
            slack_mention=os.getenv("SLACK_MENTION") or None,
            include_summary_in_alert=(
                os.getenv("INCLUDE_SUMMARY_IN_ALERT", "true").strip().lower() == "true"
            ),
            alert_summary_max_chars=int(os.getenv("ALERT_SUMMARY_MAX_CHARS", "500")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        )
