from datetime import date

from src.metrics import (
    compute_baseline,
    cost_per_engagement,
    cost_per_view,
    engagement_rate,
    overdue_flag,
    reaction_text,
    row_score,
    vs_baseline,
)


def _reel(pid, views, likes=100, comments=10, pinned=False):
    return {"post_id": pid, "product": "REELS", "is_pinned": pinned,
            "metrics": {"views": views, "likes": likes, "comments": comments}}


def _feed(pid, likes, comments=10, pinned=False):
    return {"post_id": pid, "product": "FEED", "media_type": "IMAGE", "is_pinned": pinned,
            "metrics": {"views": None, "likes": likes, "comments": comments}}


RECENT = [
    _reel("r1", 1000), _reel("r2", 2000), _reel("r3", 3000),
    _reel("r4", 50000, pinned=True),          # 고정핀 → 제외
    _reel("spon", 99999),                     # 협찬 → 제외
    _feed("f1", 100), _feed("f2", 200), _feed("f3", 300),
]


def test_baseline_reels_excludes_sponsored_and_pinned():
    b = compute_baseline(RECENT, {"spon"}, "릴스")
    assert b["n"] == 3
    assert b["views_median"] == 2000


def test_baseline_feed_uses_engagement():
    b = compute_baseline(RECENT, {"spon"}, "피드")
    assert b["n"] == 3
    assert b["eng_median"] == 210  # 200 + 10


def test_baseline_insufficient():
    assert compute_baseline([_reel("r1", 1000)], set(), "릴스") is None
    assert compute_baseline([], set(), "피드") is None


def test_vs_baseline():
    b = {"views_median": 2000, "eng_median": 210}
    assert vs_baseline(_reel("x", 4000), b) == 2.0
    assert vs_baseline(_feed("y", 200), b) == 1.0
    assert vs_baseline(_reel("x", 4000), None) is None


def test_engagement_rate():
    assert engagement_rate({"likes": 300, "comments": 40}, 10000) == 0.034
    assert engagement_rate({"likes": 300, "comments": 40}, None) is None
    assert engagement_rate({"likes": None, "comments": None}, 10000) is None


def test_cost_efficiency():
    assert cost_per_engagement(390000, {"likes": 900, "comments": 100}) == 390.0
    assert cost_per_engagement(390000, {"likes": 0, "comments": 0}) is None
    assert cost_per_engagement(None, {"likes": 900, "comments": 100}) is None
    assert cost_per_view(100000, {"views": 50000}) == 2.0
    assert cost_per_view(100000, {"views": None}) is None


def test_row_score():
    posts = [{"computed": {"vs_baseline": 2.0}}, {"computed": {"vs_baseline": 1.0}}]
    assert row_score(posts) == 150
    assert row_score([{"computed": {}}]) is None
    assert row_score([]) is None


def test_overdue_flag():
    today = date(2026, 7, 23)
    assert overdue_flag("진행 중", "2026-07-10", 0, today) is True
    assert overdue_flag("진행 중", "2026-07-16", 0, today) is False  # 정확히 +7일
    assert overdue_flag("진행 중", "2026-07-15", 0, today) is True   # +8일
    assert overdue_flag("진행 중", "2026-07-10", 2, today) is False  # URL 있음
    assert overdue_flag("종료", "2026-07-01", 0, today) is False
    assert overdue_flag("진행 중", None, 0, today) is False


def test_reaction_text():
    post = {
        "product": "REELS", "frozen": False, "days_since_post": 14,
        "metrics": {"views": 12345, "likes": 678, "comments": 90},
        "computed": {"er": 0.034, "vs_baseline": 2.13},
    }
    assert reaction_text(post) == "조회 12,345 · 좋아요 678 · 댓글 90 · ER 3.4% · 평소대비 2.1x (D+14)"
    assert reaction_text({"metrics": {}, "computed": {}}) == "지표 수집 실패"
    frozen = dict(post, frozen=True)
    assert reaction_text(frozen).endswith("(확정)")
