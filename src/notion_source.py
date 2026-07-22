"""노션 '협찬 데이터' DB에서 협찬 행 + 각 행 페이지의 '결과물' DB를 읽는다.

구조: 메인 DB 행(협찬 계약 1건) → 행 페이지 안에 인라인 '결과물' DB(행마다 별개 DB id)
      → 결과물 행에 협찬 게시물 URL·유형(피드/릴스)·업로드일.
"""

from __future__ import annotations

import logging
import os

import requests

API = "https://api.notion.com/v1"
log = logging.getLogger(__name__)


def _headers(version: str) -> dict:
    return {
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    }


def _plain_text(prop: dict) -> str:
    arr = prop.get("title") or prop.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in arr).strip()


def _multi_select(prop: dict) -> list[str]:
    return [o.get("name", "") for o in prop.get("multi_select") or []]


def _select(prop: dict) -> str | None:
    v = prop.get("select") or prop.get("status")
    return v.get("name") if v else None


def _date_start(prop: dict) -> str | None:
    d = prop.get("date")
    return d.get("start") if d else None


def _query_db(db_id: str, version: str, payload: dict | None = None) -> list[dict]:
    """DB 쿼리 전체 페이지네이션."""
    payload = dict(payload or {})
    pages: list[dict] = []
    while True:
        res = requests.post(f"{API}/databases/{db_id}/query",
                            headers=_headers(version), json=payload, timeout=60)
        res.raise_for_status()
        body = res.json()
        pages.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        payload["start_cursor"] = body["next_cursor"]
    return pages


def fetch_sponsor_rows(db_id: str, version: str) -> list[dict]:
    """진행 중·종료 상태의 협찬 행 목록."""
    payload = {"filter": {"or": [
        {"property": "진행상태", "status": {"equals": "진행 중"}},
        {"property": "진행상태", "status": {"equals": "종료"}},
    ]}}
    rows = []
    for page in _query_db(db_id, version, payload):
        p = page["properties"]
        row = {
            "page_id": page["id"],
            "row_title_raw": _plain_text(p.get("ID", {})),
            "status": _select(p.get("진행상태", {})),
            "media": _multi_select(p.get("매체", {})),
            "brands": _multi_select(p.get("브랜드", {})),
            "manager": _select(p.get("담당자", {})),
            "followers_notion_raw": _plain_text(p.get("팔로워", {})),
            "product_value_krw": (p.get("상품 가액") or {}).get("number"),
            "delivery_date": _date_start(p.get("상품 배송일", {})),
            "output_link_prop": (p.get("산출물  링크 (1)") or {}).get("url"),
        }
        if row["row_title_raw"]:
            rows.append(row)
    return rows


def find_output_db_id(page_id: str, version: str) -> str | None:
    """행 페이지 블록에서 인라인 '결과물' child_database id 를 찾는다."""
    url = f"{API}/blocks/{page_id}/children?page_size=100"
    while True:
        res = requests.get(url, headers=_headers(version), timeout=60)
        res.raise_for_status()
        body = res.json()
        for block in body.get("results", []):
            if block.get("type") == "child_database":
                return block["id"]
        if not body.get("has_more"):
            return None
        url = f"{API}/blocks/{page_id}/children?page_size=100&start_cursor={body['next_cursor']}"


def fetch_output_rows(output_db_id: str, version: str) -> list[dict]:
    """결과물 DB 행들. URL 유무와 무관하게 모두 반환 (URL 없는 행은 업로드 누락 판정용)."""
    try:
        pages = _query_db(output_db_id, version)
    except requests.HTTPError as e:
        log.warning("결과물 DB 쿼리 실패 %s: %s", output_db_id, e)
        return []
    rows = []
    for page in pages:
        p = page["properties"]
        rows.append({
            "notion_row_id": page["id"],
            "title": _plain_text(p.get("제품", {})),
            "url": (p.get("URL") or {}).get("url"),
            "media_kind": _select(p.get("선택", {})),
            "upload_date_notion": _date_start(p.get("업로드일", {})),
            "reaction_existing": _plain_text(p.get("반응도", {})),
        })
    return rows
