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


def ensure_output_props(db_id: str, version: str) -> bool:
    """구형 결과물 DB에 반응도/반응체크일 속성을 추가한다 (기존 속성·데이터는 그대로)."""
    res = requests.patch(
        f"{API}/databases/{db_id}",
        headers=_headers(version),
        json={"properties": {
            "반응도": {"rich_text": {}},
            "반응체크일": {"date": {}},
        }},
        timeout=60,
    )
    if not res.ok:
        log.warning("결과물 DB 속성 추가 실패 %s: %s", db_id, res.text[:200])
    return res.ok


def update_output_row(row_id: str, reaction: str, check_date: str, version: str,
                      db_id: str | None = None) -> bool:
    """결과물 행에 반응도 텍스트 + 반응체크일 기입. 성공 여부 반환.

    구형 결과물 DB 는 반응도/반응체크일 속성이 없어 400 이 난다 →
    속성을 추가하고 1회 재시도.
    """
    payload = {"properties": {
        "반응도": {"rich_text": _rt(reaction)},
        "반응체크일": {"date": {"start": check_date}},
    }}
    res = requests.patch(f"{API}/pages/{row_id}", headers=_headers(version),
                         json=payload, timeout=60)
    if not res.ok and "is not a property" in res.text and db_id:
        if ensure_output_props(db_id, version):
            res = requests.patch(f"{API}/pages/{row_id}", headers=_headers(version),
                                 json=payload, timeout=60)
    if not res.ok:
        log.warning("결과물 행 기입 실패 %s: %s", row_id, res.text[:200])
    return res.ok


def update_hub_status(page_id: str, text: str, version: str) -> bool:
    """허브 페이지 최상단 콜아웃에 최근 실행 요약 1줄 기입.

    콜아웃이 없으면 페이지 끝에 새로 만든다 (노션 API 는 맨 앞 삽입 미지원 —
    최초 1회만 수동으로 위치를 잡아주면 이후엔 그 블록을 계속 갱신).
    """
    res = requests.get(f"{API}/blocks/{page_id}/children?page_size=30",
                       headers=_headers(version), timeout=60)
    if not res.ok:
        log.warning("허브 블록 조회 실패 %s: %s", page_id, res.text[:200])
        return False
    callout = next((b for b in res.json().get("results", [])
                    if b.get("type") == "callout"), None)
    if callout:
        r = requests.patch(f"{API}/blocks/{callout['id']}", headers=_headers(version),
                           json={"callout": {"rich_text": _rt(text)}}, timeout=60)
    else:
        r = requests.patch(
            f"{API}/blocks/{page_id}/children", headers=_headers(version),
            json={"children": [{"object": "block", "type": "callout", "callout": {
                "rich_text": _rt(text), "icon": {"emoji": "🔄"},
                "color": "gray_background"}}]}, timeout=60)
    if not r.ok:
        log.warning("허브 콜아웃 기입 실패: %s", r.text[:200])
    return r.ok


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
