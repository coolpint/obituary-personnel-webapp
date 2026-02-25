from __future__ import annotations

from dataclasses import dataclass

import requests

from .classifier import ClassificationResult
from .rss_fetcher import RSSItem


def build_alert_message(item: RSSItem, result: ClassificationResult) -> str:
    tags = [result.category]
    if result.is_media_related:
        tags.append("언론인")
    tag_text = "][".join(tags)

    details = []
    if result.matched_roles:
        details.append(f"직군: {', '.join(result.matched_roles)}")
    if result.matched_outlets:
        details.append(f"언론사: {', '.join(result.matched_outlets[:5])}")

    detail_text = "\n".join(details)
    if detail_text:
        detail_text = f"\n{detail_text}"

    return (
        f"[{tag_text}] {item.title}\n"
        f"시간: {item.published_at}\n"
        f"링크: {item.link}{detail_text}"
    )


class Notifier:
    channel_name = "noop"

    def send(self, message: str) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    channel_name = "console"

    def send(self, message: str) -> None:
        print(message, flush=True)


@dataclass
class SlackWebhookNotifier(Notifier):
    webhook_url: str
    timeout_seconds: int = 15
    mention: str | None = None
    channel_name: str = "slack"

    def send(self, message: str) -> None:
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        headline = lines[0] if lines else "언론인 알림"
        body = "\n".join(lines[1:]) if len(lines) > 1 else ""

        text = f"{self.mention} {headline}".strip() if self.mention else headline
        payload = {
            "text": text,
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": headline[:150]},
                },
            ],
        }
        if self.mention:
            payload["blocks"].insert(
                0,
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": self.mention},
                },
            )
        if body:
            payload["blocks"].append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": body[:2800]},
                }
            )

        response = requests.post(
            self.webhook_url,
            timeout=self.timeout_seconds,
            json=payload,
        )
        response.raise_for_status()


@dataclass
class TelegramNotifier(Notifier):
    bot_token: str
    chat_id: str
    timeout_seconds: int = 15
    channel_name: str = "telegram"

    def send(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        response = requests.post(
            url,
            timeout=self.timeout_seconds,
            json={
                "chat_id": self.chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
