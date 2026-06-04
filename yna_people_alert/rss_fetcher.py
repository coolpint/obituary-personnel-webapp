from __future__ import annotations

from dataclasses import dataclass
from typing import List
import re
import time
import xml.etree.ElementTree as ET

import requests


@dataclass(frozen=True)
class RSSItem:
    guid: str
    title: str
    summary: str
    link: str
    published_at: str
    raw_xml: str


def _text(node: ET.Element | None, tag: str, default: str = "") -> str:
    if node is None:
        return default
    child = node.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _parse_rss_xml(xml_text: str) -> ET.Element:
    """Parse RSS XML, tolerating occasional unescaped ampersands in feed URLs."""
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError:
        # YNA sometimes emits media URLs such as
        #   ...youtube.com/embed/...?...&start=126
        # inside XML attributes without escaping the ampersand. That makes the
        # whole feed invalid even though the item content is otherwise usable.
        sanitized = re.sub(
            r"&(?!#\d+;|#x[0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)",
            "&amp;",
            xml_text,
        )
        return ET.fromstring(sanitized)


def fetch_rss_items(url: str, timeout_seconds: int = 15) -> List[RSSItem]:
    headers = {
        "User-Agent": "YNA-People-Alert/1.0 (+https://github.com/coolpint/obituary-personnel-webapp)"
    }
    last_error: requests.exceptions.RequestException | None = None

    for attempt in range(3):
        try:
            response = requests.get(url, timeout=timeout_seconds, headers=headers)
            response.raise_for_status()
            break
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    else:
        raise last_error or RuntimeError("Failed to fetch RSS")

    xml_text = response.text
    root = _parse_rss_xml(xml_text)
    items: List[RSSItem] = []

    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        summary = _text(item, "description")
        link = _text(item, "link")
        guid = _text(item, "guid") or f"{link}|{_text(item, 'pubDate')}"
        published_at = _text(item, "pubDate")

        item_xml = ET.tostring(item, encoding="unicode")
        items.append(
            RSSItem(
                guid=guid,
                title=title,
                summary=summary,
                link=link,
                published_at=published_at,
                raw_xml=item_xml,
            )
        )
    return items

