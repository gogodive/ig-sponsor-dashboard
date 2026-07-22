"""성과 지표 계산 (순수 함수 — API/파일 접근 없음).

베이스라인 = 그 계정의 협찬·고정핀 제외 같은 유형 최근 게시물 중앙값.
릴스는 조회수, 피드는 참여(좋아요+댓글) 기준으로 비교한다 —
조회수는 릴스에만 공개되는 지표라 피드와 섞으면 왜곡된다.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta


def is_reel_post(post: dict) -> bool:
    return post.get("product") == "REELS" or post.get("media_type") == "VIDEO"


def engagement(metrics: dict) -> int | None:
    likes, comments = metrics.get("likes"), metrics.get("comments")
    if likes is None and comments is None:
        return None
    return (likes or 0) + (comments or 0)


def engagement_rate(metrics: dict, followers: int | None) -> float | None:
    """(좋아요+댓글)/팔로워. %가 아니라 비율 (0.034 = 3.4%)."""
    eng = engagement(metrics)
    if eng is None or not followers:
        return None
    return eng / followers


def compute_baseline(recent_posts: list[dict], sponsored_shortcodes: set[str],
                     media_kind: str, max_posts: int = 12,
                     min_posts: int = 3) -> dict | None:
    """계정 평소 성과 기준치. media_kind: '릴스' 또는 '피드'.

    협찬 게시물(이 계정의 모든 협찬 행 합집합)·고정핀 제외, 같은 유형만.
    반환: {views_median, eng_median, n} — n < min_posts 면 None.
    """
    want_reel = media_kind == "릴스"
    pool = [p for p in recent_posts
            if p.get("post_id") not in sponsored_shortcodes
            and not p.get("is_pinned")
            and is_reel_post(p) == want_reel][:max_posts]
    views = [p["metrics"]["views"] for p in pool
             if isinstance(p.get("metrics", {}).get("views"), int) and p["metrics"]["views"] > 0]
    engs = [e for e in (engagement(p.get("metrics", {})) for p in pool)
            if isinstance(e, int) and e > 0]
    key_vals = views if want_reel else engs
    if len(key_vals) < min_posts:
        return None
    return {
        "views_median": statistics.median(views) if views else None,
        "eng_median": statistics.median(engs) if engs else None,
        "n": len(key_vals),
    }


def vs_baseline(post: dict, baseline: dict | None) -> float | None:
    """평소 대비 배수. 릴스=조회수/중앙값, 피드=참여/중앙값."""
    if not baseline:
        return None
    m = post.get("metrics", {})
    if is_reel_post(post):
        views, med = m.get("views"), baseline.get("views_median")
        if isinstance(views, int) and med:
            return views / med
        return None
    eng, med = engagement(m), baseline.get("eng_median")
    if isinstance(eng, int) and med:
        return eng / med
    return None


def cost_per_engagement(value_krw: float | None, metrics: dict) -> float | None:
    eng = engagement(metrics)
    if not value_krw or not eng:
        return None
    return value_krw / eng


def cost_per_view(value_krw: float | None, metrics: dict) -> float | None:
    views = metrics.get("views")
    if not value_krw or not isinstance(views, int) or views <= 0:
        return None
    return value_krw / views


def row_score(posts: list[dict]) -> int | None:
    """협찬 행 종합 점수 = 게시물 vs_baseline 평균 × 100 (100 = 계정 평소 수준).

    노션 메인 행 '반응도' 숫자 필드에 기록되는 값.
    """
    ratios = [p.get("computed", {}).get("vs_baseline") for p in posts]
    ratios = [r for r in ratios if isinstance(r, (int, float))]
    if not ratios:
        return None
    return round(sum(ratios) / len(ratios) * 100)


def overdue_flag(status: str, delivery_date: str | None, output_url_count: int,
                 today: date, overdue_days: int = 7) -> bool:
    """진행 중인데 배송일+N일 지나도록 결과물 URL이 하나도 없으면 True."""
    if status != "진행 중" or output_url_count > 0 or not delivery_date:
        return False
    try:
        d = date.fromisoformat(delivery_date[:10])
    except ValueError:
        return False
    return today > d + timedelta(days=overdue_days)


def reaction_text(post: dict) -> str:
    """노션 결과물 행 '반응도'에 기록할 요약 텍스트."""
    m = post.get("metrics", {})
    c = post.get("computed", {})
    parts = []
    if isinstance(m.get("views"), int):
        parts.append(f"조회 {m['views']:,}")
    if m.get("likes") is not None:
        parts.append(f"좋아요 {m['likes']:,}")
    if m.get("comments") is not None:
        parts.append(f"댓글 {m['comments']:,}")
    if c.get("er") is not None:
        parts.append(f"ER {c['er'] * 100:.1f}%")
    if c.get("vs_baseline") is not None:
        parts.append(f"평소대비 {c['vs_baseline']:.1f}x")
    txt = " · ".join(parts) if parts else "지표 수집 실패"
    days = post.get("days_since_post")
    if post.get("frozen"):
        txt += " (확정)"
    elif days is not None:
        txt += f" (D+{days})"
    return txt
