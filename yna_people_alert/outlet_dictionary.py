from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Set

import requests
from bs4 import BeautifulSoup


SEED_OUTLETS = {
    "연합뉴스",
    "뉴시스",
    "뉴스1",
    "한국경제",
    "매일경제",
    "서울경제",
    "아시아경제",
    "머니투데이",
    "헤럴드경제",
    "파이낸셜뉴스",
    "조선일보",
    "중앙일보",
    "동아일보",
    "한겨레",
    "경향신문",
    "한국일보",
    "서울신문",
    "국민일보",
    "세계일보",
    "문화일보",
    "전자신문",
    "디지털타임스",
    "이데일리",
    "오마이뉴스",
    "SBS",
    "KBS",
    "MBC",
    "JTBC",
    "TV조선",
    "채널A",
    "MBN",
    "YTN",
    "연합인포맥스",
    "미디어오늘",
    "프레시안",
    "시사IN",
}

NON_OUTLET_TERMS = {
    "뉴스",
    "홈",
    "전체",
    "랭킹",
    "속보",
    "정치",
    "경제",
    "사회",
    "생활",
    "문화",
    "세계",
    "연예",
    "스포츠",
    "포토",
    "동영상",
    "TV",
    "LIVE",
    "AI",
    "연재",
    "기획",
    "칼럼",
    "오피니언",
    "기자",
}


def normalize_name(name: str) -> str:
    text = name.strip()
    text = text.replace("(주)", "").replace("㈜", "")
    text = re.sub(r"\s+", "", text)
    return text


def _looks_like_outlet_name(name: str) -> bool:
    if len(name) < 2 or len(name) > 20:
        return False
    if name in NON_OUTLET_TERMS:
        return False
    return bool(re.search(r"[가-힣A-Za-z]", name))


def _looks_like_outlet_href(href: str) -> bool:
    return bool(
        re.search(
            r"(officeId=\d+|/main/office\.naver|/cp/|/channel/|/media/)",
            href,
        )
    )


def _extract_outlet_names_from_html(html: str, strict_href: bool = False) -> Set[str]:
    soup = BeautifulSoup(html, "html.parser")
    names: Set[str] = set()

    for link in soup.select("a"):
        href = (link.get("href") or "").strip()
        if strict_href and href and not _looks_like_outlet_href(href):
            continue
        text = link.get_text(strip=True)
        if not text:
            continue
        normalized = normalize_name(text)
        if _looks_like_outlet_name(normalized):
            names.add(normalized)

    # Fallback regex scan for layout/script changes.
    if len(names) < 20:
        for raw in re.findall(r">([가-힣A-Za-z0-9\s]{2,30})<", html):
            normalized = normalize_name(raw)
            if _looks_like_outlet_name(normalized):
                names.add(normalized)

    return names


def _fetch_outlet_names(url: str, timeout_seconds: int = 15, strict_href: bool = False) -> Set[str]:
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return _extract_outlet_names_from_html(response.text, strict_href=strict_href)


def fetch_naver_outlet_names(url: str, timeout_seconds: int = 15) -> Set[str]:
    return _fetch_outlet_names(url, timeout_seconds=timeout_seconds, strict_href=True)


def fetch_daum_outlet_names(url: str, timeout_seconds: int = 15) -> Set[str]:
    return _fetch_outlet_names(url, timeout_seconds=timeout_seconds, strict_href=True)


def _merge_names(*groups: Iterable[str]) -> Set[str]:
    merged: Set[str] = set()
    for names in groups:
        for name in names:
            normalized = normalize_name(name)
            if _looks_like_outlet_name(normalized):
                merged.add(normalized)
    return merged


@dataclass
class OutletDictionary:
    cache_path: Path
    naver_office_list_url: str
    daum_cplist_url: str
    refresh_hours: int = 12
    timeout_seconds: int = 15
    names: Set[str] | None = None

    def load(self) -> Set[str]:
        names = set(normalize_name(x) for x in SEED_OUTLETS if x.strip())

        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                for name in data.get("names", []):
                    normalized = normalize_name(name)
                    if _looks_like_outlet_name(normalized):
                        names.add(normalized)
            except Exception:
                pass

        self.names = names
        return names

    def _needs_refresh(self) -> bool:
        if not self.cache_path.exists():
            return True
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            updated_at = data.get("updated_at")
            if not updated_at:
                return True
            updated_dt = datetime.fromisoformat(updated_at)
            now = datetime.now(timezone.utc)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            return now - updated_dt > timedelta(hours=self.refresh_hours)
        except Exception:
            return True

    def refresh_if_needed(self) -> Set[str]:
        if self.names is None:
            self.load()

        if not self._needs_refresh():
            return self.names or set()

        names = set(self.names or set())
        try:
            naver_names = fetch_naver_outlet_names(
                self.naver_office_list_url, timeout_seconds=self.timeout_seconds
            )
            daum_names = fetch_daum_outlet_names(
                self.daum_cplist_url, timeout_seconds=self.timeout_seconds
            )
            names = _merge_names(names, naver_names, daum_names)
            self._save_cache(names)
        except Exception:
            # Keep last known names (seed + cache) if network or parsing fails.
            pass

        self.names = names
        return names

    def _save_cache(self, names: Set[str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": {
                "naver": self.naver_office_list_url,
                "daum": self.daum_cplist_url,
            },
            "names": sorted(names),
        }
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
