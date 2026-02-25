from __future__ import annotations

import html
import re


BYLINE_PATTERNS = [
    # (서울=연합뉴스), (부산=뉴시스), (도쿄=연합뉴스) 등 지역/매체가 바뀌는 바이라인
    re.compile(r"^\s*\([^)]*=[^)]+\)\s*[^=\n]{1,40}?(?:기자|특파원|논설위원)\s*=\s*"),
    # 홍길동 기자 = ...
    re.compile(r"^\s*[^=\n]{1,30}?(?:기자|특파원|논설위원)\s*=\s*"),
]


def strip_leading_byline(text: str) -> str:
    lines = []
    for line in text.splitlines():
        cleaned = line.strip()
        for pattern in BYLINE_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        lines.append(cleaned)
    return "\n".join(lines).strip()


def clean_rss_text(text: str) -> str:
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = cleaned.replace("\xa0", " ")
    cleaned = strip_leading_byline(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

