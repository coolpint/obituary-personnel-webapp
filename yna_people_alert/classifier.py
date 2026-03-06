from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, List, Sequence, Set

from .outlet_dictionary import normalize_name
from .text_utils import strip_leading_byline


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

MEDIA_CONTEXT_ROLE_KEYWORDS = {
    "기자",
    "특파원",
    "논설위원",
    "앵커",
    "PD",
    "피디",
    "부장",
    "차장",
    "국장",
    "편집장",
    "편집국장",
    "보도국장",
    "주필",
    "팀장",
}

OUTLET_NAME_SUFFIX_HINTS = {
    "신문",
    "일보",
    "뉴스",
    "비즈",
    "방송",
    "타임스",
    "헤럴드",
    "저널",
    "미디어",
    "프레스",
    "데일리",
    "경제",
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


def _find_context_roles(normalized_text: str, outlets: Sequence[str] | Set[str]) -> List[str]:
    hits = set()
    for outlet in set(outlets):
        if not outlet:
            continue
        for role in MEDIA_CONTEXT_ROLE_KEYWORDS:
            # Detect patterns like "조선비즈테크부장", "한겨레신문편집국장".
            pattern = re.escape(outlet) + r".{0,8}" + re.escape(role)
            if re.search(pattern, normalized_text):
                hits.add(role)
    return sorted(hits)


def _looks_like_outlet_name_hint(token: str) -> bool:
    normalized = normalize_name(token)
    if len(normalized) < 2 or len(normalized) > 20:
        return False
    if normalized in {"KBS", "MBC", "SBS", "JTBC", "YTN", "MBN", "TV조선", "채널A"}:
        return True
    return any(normalized.endswith(suffix) for suffix in OUTLET_NAME_SUFFIX_HINTS)


def _find_outlet_hints_from_parentheses(text: str) -> List[str]:
    hits = set()
    role_pattern = (
        r"(?:기자|특파원|논설위원|앵커|PD|피디|부장|차장|국장|편집장|편집국장|보도국장|주필|팀장)"
    )
    head_pattern = re.compile(
        r"^\s*([가-힣A-Za-z0-9]{2,20})\s*[가-힣A-Za-z0-9]{0,10}" + role_pattern + r"\s*$"
    )
    for seg in re.findall(r"\(([^)]{2,60})\)", text):
        if "=" in seg:
            continue
        segment = seg.strip()
        if not segment:
            continue

        # Case: "(조선비즈 테크부장)" -> "조선비즈".
        head_match = head_pattern.match(segment)
        if head_match:
            cand = normalize_name(head_match.group(1))
            if _looks_like_outlet_name_hint(cand):
                hits.add(cand)
            continue

        # Case: "(조선비즈)" or "(아시아경제)".
        first_token = re.split(r"[\s,·;/]+", segment)[0]
        cand = normalize_name(first_token)
        if _looks_like_outlet_name_hint(cand):
            hits.add(cand)
    return sorted(hits)


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
    cleaned_text = strip_leading_byline(text)

    category, category_hits = classify_category(cleaned_text)

    normalized_text = normalize_name(cleaned_text)
    outlet_hits = sorted(
        set(outlet for outlet in set(outlets) if outlet and outlet in normalized_text)
        | set(_find_outlet_hints_from_parentheses(cleaned_text))
    )
    role_hits = sorted(
        set(_find_keywords(cleaned_text, MEDIA_ROLE_KEYWORDS))
        | set(_find_context_roles(normalized_text, outlet_hits))
    )

    score = min(len(role_hits) * 2, 4) + min(len(outlet_hits) * 3, 9)
    if role_hits and outlet_hits:
        score += 2

    is_media_related = score >= threshold
    # For people-obituary/personnel feeds, outlet mention itself is a strong media signal.
    if category in {"인사", "부고"} and outlet_hits:
        is_media_related = True
        score = max(score, threshold)

    return ClassificationResult(
        category=category,
        matched_category_keywords=category_hits,
        media_score=score,
        is_media_related=is_media_related,
        matched_roles=role_hits,
        matched_outlets=outlet_hits,
    )
