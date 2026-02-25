from __future__ import annotations

from dataclasses import dataclass
from typing import List
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


def fetch_rss_items(url: str, timeout_seconds: int = 15) -> List[RSSItem]:
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()

    xml_text = response.text
    root = ET.fromstring(xml_text)
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

