"""노션 자유입력 텍스트 정리용 순수 함수 — username 정규화·shortcode 추출·팔로워 파싱."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_VALID_USERNAME = re.compile(r"^[a-z0-9._]{1,30}$")
# 게시물 경로: /p/, /reel/, /reels/, /tv/ 또는 /{username}/p/ 형태
_SHORTCODE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})")


def normalize_username(raw: str | None) -> str | None:
    """노션 ID 타이틀에서 인스타 username 추출. 실패 시 None.

    실제 사례: '*heynana_o3o', 'a_aj_j  / 계약 취소', 'ann.heidi.mc( healthylife.heidi)',
    '**__1_7_4mon**' (노션 볼드 마크업 잔재).
    """
    if not raw:
        return None
    s = raw.strip()
    # 구분자(슬래시·괄호·콤마·공백 연속) 이후는 메모로 간주하고 절단
    s = re.split(r"[/(,\s]{2,}|[/(,]", s, maxsplit=1)[0]
    s = s.strip().strip("*@").rstrip(".")  # 장식 문자 제거 (선행 *·@, 후행 마침표)
    s = s.lower()
    return s if _VALID_USERNAME.match(s) else None


def extract_shortcode(url: str | None) -> str | None:
    """인스타 게시물 URL → shortcode. 게시물 URL이 아니면 None."""
    if not url:
        return None
    url = url.strip()
    if "instagram.com" not in url:
        return None
    m = _SHORTCODE.search(urlparse(url).path)
    return m.group(1) if m else None


def parse_follower_count(raw: str | None) -> int | None:
    """'19000' / '19,000' / '1.9만' / '19k' → int. 실패 시 None."""
    if not raw:
        return None
    s = str(raw).strip().replace(",", "").lower()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(만|천|k|m)?$", s)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"만": 10_000, "천": 1_000, "k": 1_000, "m": 1_000_000}.get(m.group(2), 1)
    return int(num * mult)
