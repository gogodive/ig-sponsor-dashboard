"""Apify Instagram Scraper 로 타사 공개 계정의 최신 게시물을 수집한다.

공개 지표만 수집 가능: 조회수(릴스/영상)·좋아요·댓글·팔로워.
저장·공유·도달은 계정 주인만 볼 수 있어 어떤 방법으로도 수집 불가.
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.apify.com/v2"
POLL_INTERVAL_S = 10
RUN_TIMEOUT_S = 900  # 계정당 최대 대기 (무거운 계정 대비)


def _run_actor(actor: str, payload: dict, timeout_s: int = RUN_TIMEOUT_S) -> list:
    """actor 를 비동기로 실행하고 완료까지 폴링 후 결과를 받는다.

    run-sync 엔드포인트는 ~300초를 넘기면 서버가 연결을 끊어버려
    게시물이 많은 계정에서 실패한다 → 비동기 실행 + 폴링으로 대체.
    """
    token = os.environ["APIFY_TOKEN"]
    res = requests.post(f"{API}/acts/{actor}/runs", params={"token": token},
                        json=payload, timeout=60)
    res.raise_for_status()
    run_id = res.json()["data"]["id"]

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        st = requests.get(f"{API}/actor-runs/{run_id}", params={"token": token}, timeout=60)
        st.raise_for_status()
        data = st.json()["data"]
        status = data["status"]
        if status == "SUCCEEDED":
            items = requests.get(
                f"{API}/datasets/{data['defaultDatasetId']}/items",
                params={"token": token, "clean": "true", "limit": 1000}, timeout=180)
            items.raise_for_status()
            return items.json()
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {status} (run_id={run_id})")

    # 시간 초과 → 크레딧 낭비 방지를 위해 중단 요청
    try:
        requests.post(f"{API}/actor-runs/{run_id}/abort", params={"token": token}, timeout=60)
    except requests.RequestException:
        pass
    raise RuntimeError(f"Apify run 폴링 시간 초과 {timeout_s}s (run_id={run_id})")


def _map_post(m: dict) -> dict:
    t = (m.get("type") or m.get("productType") or "").lower()
    is_reel = t in ("reel", "clips") or m.get("productType") == "clips"
    if is_reel or t == "video":
        mtype = "VIDEO"
    elif t in ("sidecar", "carousel"):
        mtype = "CAROUSEL_ALBUM"
    else:
        mtype = "IMAGE"
    return {
        "post_id": m.get("shortCode") or m.get("id") or m.get("url"),
        "caption": (m.get("caption") or "")[:300],
        "media_type": mtype,
        "product": "REELS" if is_reel else "FEED",
        "permalink": m.get("url"),
        "thumbnail": m.get("displayUrl"),
        "posted_at": m.get("timestamp"),
        "is_pinned": bool(m.get("isPinned")),
        "owner_username": m.get("ownerUsername"),
        "metrics": {
            # 조회수 필드명이 actor 버전/게시물 유형에 따라 달라 폭넓게 폴백
            "views": (m.get("videoPlayCount") or m.get("videoViewCount")
                      or m.get("videoViews") or m.get("igPlayCount") or m.get("playCount")),
            "likes": m.get("likesCount"),
            "comments": m.get("commentsCount"),
        },
    }


def fetch_followers(username: str, actor: str) -> int | None:
    """팔로워 수만 초경량 조회 (details 1건). posts 모드엔 팔로워가 없어서 별도 호출."""
    payload = {
        "directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": "details",
        "resultsLimit": 1,
        "addParentData": False,
    }
    items = _run_actor(actor, payload, timeout_s=300)
    if not items:
        return None
    return items[0].get("followersCount") or items[0].get("ownerFollowersCount")


def fetch_posts_by_urls(post_urls: list[str], actor: str) -> list[dict]:
    """게시물 URL 직접 조회 (최근 수집 창에 없는 협찬 게시물용). 전 계정 모아 한 번에.

    반환: _map_post 형식 리스트. 실패/삭제된 게시물은 결과에 없을 수 있다.
    """
    if not post_urls:
        return []
    payload = {
        "directUrls": post_urls,
        "resultsType": "posts",
        "resultsLimit": len(post_urls),
        "addParentData": False,
    }
    items = _run_actor(actor, payload)
    posts = [_map_post(m) for m in items
             if m.get("shortCode") or m.get("type") or m.get("url")]
    return [p for p in posts if p["post_id"] and p["posted_at"]]


def fetch_account(username: str, actor: str, results_type: str, limit: int) -> dict:
    """한 계정의 스냅샷: {followers_count, posts:[...]}. posts 는 최신순."""
    payload = {
        "directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": results_type,
        "resultsLimit": limit,
        "addParentData": False,
    }
    items = _run_actor(actor, payload)
    if not items:
        return {"followers_count": None, "posts": []}

    head = items[0]
    followers = head.get("followersCount") or head.get("ownerFollowersCount")
    raw = head.get("latestPosts") if isinstance(head.get("latestPosts"), list) else None
    if not raw:
        raw = [it for it in items if it.get("type") or it.get("shortCode") or it.get("url")]
    posts = [_map_post(m) for m in raw[:limit]]
    posts = [p for p in posts if p["post_id"] and p["posted_at"]]
    return {"followers_count": followers, "posts": posts}
