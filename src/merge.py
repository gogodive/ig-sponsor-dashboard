"""협찬 게시물 상태 병합·동결·일별 히스토리 (순수 함수 — API/파일 접근 없음).

ig-ref-dashboard 의 merge 규칙을 협찬 게시물 단위로 변형:
- 게시 후 freeze_days 까지 매일 지표 갱신 + history 업서트(같은 날 재실행 멱등)
- 동결 후엔 저장 지표 유지 (지표가 한 번도 없으면 최초 1회 백필)
- None 수집값으로 저장값을 절대 덮어쓰지 않음
- baseline 은 live 동안 매일 갱신, 동결 시 함께 고정
"""

from __future__ import annotations

from datetime import datetime, timedelta

FREEZE_DAYS = 30


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("+0000", "+00:00").replace("Z", "+00:00"))


def post_age_days(post: dict, now: datetime) -> int | None:
    """게시 경과일. posted_at(Apify) → upload_date_notion → first_seen 순 폴백."""
    ts = post.get("posted_at")
    if ts:
        return (now - parse_ts(ts).astimezone(now.tzinfo)).days
    d = post.get("upload_date_notion") or post.get("first_seen")
    if d:
        posted = datetime.fromisoformat(d[:10]).replace(tzinfo=now.tzinfo)
        return (now - posted).days
    return None


def is_frozen(post: dict, now: datetime, freeze_days: int = FREEZE_DAYS) -> bool:
    age = post_age_days(post, now)
    return age is not None and age > freeze_days


def merge_sponsored_post(stored: dict, fresh: dict | None, baseline: dict | None,
                         now: datetime, freeze_days: int = FREEZE_DAYS) -> dict:
    """협찬 게시물 하나의 저장 상태에 이번 수집분을 반영해 반환.

    stored: 저장된 post dict (최초엔 노션에서 만든 뼈대 — url/shortcode/media_kind 등).
    fresh: 이번 Apify 수집분 (_map_post 형식) 또는 None(수집 실패/미발견).
    """
    post = dict(stored)
    post.setdefault("first_seen", now.strftime("%Y-%m-%d"))

    if fresh:
        # 게시 시각·썸네일·본문은 항상 최신값으로 (CDN 서명 URL 만료 대응)
        for k in ("posted_at", "permalink", "thumbnail", "caption",
                  "media_type", "product", "owner_username"):
            if fresh.get(k) is not None:
                post[k] = fresh[k]

    frozen = is_frozen(post, now, freeze_days)
    has_stored_metrics = bool(post.get("metrics_updated_at"))

    if fresh and (not frozen or not has_stored_metrics):
        old_metrics = post.get("metrics", {})
        fresh_metrics = {k: v for k, v in fresh.get("metrics", {}).items() if v is not None}
        post["metrics"] = {**old_metrics, **fresh_metrics}
        post["metrics_updated_at"] = now.isoformat()
        if not frozen:
            hist = [h for h in post.get("history", []) if h.get("d") != now.strftime("%Y-%m-%d")]
            m = post["metrics"]
            hist.append({"d": now.strftime("%Y-%m-%d"), "views": m.get("views"),
                         "likes": m.get("likes"), "comments": m.get("comments")})
            hist.sort(key=lambda h: h["d"])
            post["history"] = hist
            if baseline:
                post["baseline"] = {**baseline, "captured_at": now.isoformat()}
        elif baseline and not post.get("baseline"):
            post["baseline"] = {**baseline, "captured_at": now.isoformat()}

    post["frozen"] = frozen
    post["days_since_post"] = post_age_days(post, now)
    return post
