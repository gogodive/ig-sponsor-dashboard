"""분석 결과를 노션에 기입: 결과물 행 반응도/반응체크일 + 메인 행 반응도(점수).

멱등성: 직전 기록값(저장 JSON의 last_written_*)과 같으면 PATCH 생략.
"""

from __future__ import annotations

import logging

import requests

from src.notion_source import API, _headers

log = logging.getLogger(__name__)


def _rt(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": (content or "")[:1900]}}]


def update_output_row(row_id: str, reaction: str, check_date: str, version: str) -> bool:
    """결과물 행에 반응도 텍스트 + 반응체크일 기입. 성공 여부 반환."""
    res = requests.patch(
        f"{API}/pages/{row_id}",
        headers=_headers(version),
        json={"properties": {
            "반응도": {"rich_text": _rt(reaction)},
            "반응체크일": {"date": {"start": check_date}},
        }},
        timeout=60,
    )
    if not res.ok:
        log.warning("결과물 행 기입 실패 %s: %s", row_id, res.text[:200])
    return res.ok


def update_row_score(page_id: str, score: int, version: str) -> bool:
    """메인 협찬 행 '반응도' 숫자 = row_score (100 = 계정 평소 수준)."""
    res = requests.patch(
        f"{API}/pages/{page_id}",
        headers=_headers(version),
        json={"properties": {"반응도": {"number": score}}},
        timeout=60,
    )
    if not res.ok:
        log.warning("메인 행 반응도 기입 실패 %s: %s", page_id, res.text[:200])
    return res.ok
