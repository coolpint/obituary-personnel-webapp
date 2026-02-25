from __future__ import annotations

import argparse
import logging
import time

from .classifier import classify_item
from .notifier import (
    ConsoleNotifier,
    SlackWebhookNotifier,
    TelegramNotifier,
    build_alert_message,
)
from .outlet_dictionary import OutletDictionary
from .rss_fetcher import fetch_rss_items
from .settings import Settings
from .store import Store


logger = logging.getLogger("yna_people_alert")


def build_notifier(settings: Settings):
    if settings.slack_webhook_url:
        logger.info("Notifier: slack webhook")
        return SlackWebhookNotifier(
            webhook_url=settings.slack_webhook_url,
            timeout_seconds=settings.request_timeout_seconds,
            mention=settings.slack_mention,
        )
    if settings.telegram_bot_token and settings.telegram_chat_id:
        logger.info("Notifier: telegram")
        return TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.request_timeout_seconds,
        )
    logger.info(
        "Notifier: console (set SLACK_WEBHOOK_URL or TELEGRAM_* env to enable alerts)"
    )
    return ConsoleNotifier()


def run_once(settings: Settings, store: Store, outlet_dict: OutletDictionary, notifier) -> None:
    outlets = outlet_dict.refresh_if_needed()
    logger.info("Loaded %d outlet names", len(outlets))

    items = fetch_rss_items(settings.rss_url, timeout_seconds=settings.request_timeout_seconds)
    logger.info("Fetched %d RSS items", len(items))

    for item in items:
        item_id = store.upsert_item(item)
        text = f"{item.title}\n{item.summary}"
        result = classify_item(text=text, outlets=outlets, threshold=settings.media_score_threshold)

        store.save_classification(
            item_id=item_id,
            category=result.category,
            matched_category_keywords=result.matched_category_keywords,
            media_score=result.media_score,
            is_media_related=result.is_media_related,
            matched_roles=result.matched_roles,
            matched_outlets=result.matched_outlets,
        )

        if result.category not in {"인사", "부고"}:
            continue
        if not result.is_media_related:
            continue
        if store.is_alerted(item_id, notifier.channel_name):
            continue

        message = build_alert_message(item, result)
        try:
            notifier.send(message)
            store.mark_alert(item_id, notifier.channel_name, "sent")
            logger.info("Alert sent guid=%s", item.guid)
        except Exception as exc:
            store.mark_alert(item_id, notifier.channel_name, "failed", error=str(exc))
            logger.exception("Alert failed guid=%s", item.guid)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YNA people RSS media alert MVP")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    settings = Settings.from_env()

    store = Store(settings.db_path)
    outlet_dict = OutletDictionary(
        cache_path=settings.outlet_cache_path,
        naver_office_list_url=settings.office_list_url,
        daum_cplist_url=settings.daum_cplist_url,
        refresh_hours=settings.outlet_refresh_hours,
        timeout_seconds=settings.request_timeout_seconds,
    )
    notifier = build_notifier(settings)

    try:
        if args.once:
            run_once(settings, store, outlet_dict, notifier)
            return 0

        while True:
            try:
                run_once(settings, store, outlet_dict, notifier)
            except Exception:
                logger.exception("Run loop failed")
            time.sleep(settings.poll_seconds)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
