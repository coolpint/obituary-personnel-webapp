from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, List, Sequence, Set

from .outlet_dictionary import normalize_name


APPOINTMENT_KEYWORDS = {
    "인사",
    "임명",
    "선임",
    "승진",
    "전보",
    "취임",
    "부임",
    "내정",
    "영입",
}

OBITUARY_KEYWORDS = {
    "부고",
    "별세",
    "타계",
    "영면",
    "빈소",
    "발인",
    "장례",
}

MEDIA_ROLE_KEYWORDS = {
    "기자",
    "논설위원",
    "앵커",
    "PD",
    "피디",
    "편집국장",
    "보도국장",
    "언론인",
    "취재기자",
    "특파원",
}


@dataclass(frozen=True)
class ClassificationResult:
    category: str
    matched_category_keywords: List[str]
    media_score: int
    is_media_related: bool
    matched_roles: List[str]
    matched_outlets: List[str]


def _find_keywords(text: str, keywords: Iterable[str]) -> List[str]:
    found = [kw for kw in keywords if kw in text]
    return sorted(set(found))


BYLINE_PATTERNS = [
    # (서울=연합뉴스), (부산=뉴시스), (도쿄=연합뉴스) 등 지역/매체가 바뀌는 바이라인
    re.compile(r"^\s*\([^)]*=[^)]+\)\s*[^=\n]{1,40}?(?:기자|특파원|논설위원)\s*=\s*"),
    # 홍길동 기자 = ...
    re.compile(r"^\s*[^=\n]{1,30}?(?:기자|특파원|논설위원)\s*=\s*"),
]


def _strip_leading_byline(text: str) -> str:
    lines = []
    for line in text.splitlines():
        cleaned = line.strip()
        for pattern in BYLINE_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        lines.append(cleaned)
    return "\n".join(lines).strip()


def classify_category(text: str) -> tuple[str, List[str]]:
    appointment_hits = _find_keywords(text, APPOINTMENT_KEYWORDS)
    obituary_hits = _find_keywords(text, OBITUARY_KEYWORDS)

    if len(appointment_hits) > len(obituary_hits):
        return "인사", appointment_hits
    if len(obituary_hits) > len(appointment_hits):
        return "부고", obituary_hits
    if appointment_hits:
        return "인사", appointment_hits
    if obituary_hits:
        return "부고", obituary_hits
    return "other", []


def classify_item(text: str, outlets: Sequence[str] | Set[str], threshold: int) -> ClassificationResult:
    cleaned_text = _strip_leading_byline(text)

    category, category_hits = classify_category(cleaned_text)
    role_hits = _find_keywords(cleaned_text, MEDIA_ROLE_KEYWORDS)

    normalized_text = normalize_name(cleaned_text)
    outlet_hits = sorted(
        outlet for outlet in set(outlets) if outlet and outlet in normalized_text
    )

    score = min(len(role_hits) * 2, 4) + min(len(outlet_hits) * 3, 9)
    if role_hits and outlet_hits:
        score += 2

    is_media_related = score >= threshold

    return ClassificationResult(
        category=category,
        matched_category_keywords=category_hits,
        media_score=score,
        is_media_related=is_media_related,
        matched_roles=role_hits,
        matched_outlets=outlet_hits,
    )
