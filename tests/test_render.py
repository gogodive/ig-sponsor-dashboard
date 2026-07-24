from datetime import datetime, timedelta, timezone

from src.render import render_html

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 23, 8, 0, tzinfo=KST)


def _row(**kw):
    base = {
        "page_id": "p1", "row_title_raw": "tester", "username": "tester",
        "status": "진행 중", "media": ["인스타"], "brands": ["라세린"],
        "manager": "서채윤", "followers": 19000, "product_value_krw": 390000,
        "delivery_date": "2026-07-01",
        "flags": {"overdue_no_output": False, "unresolvable_username": False,
                  "non_instagram": False, "account_unavailable": False},
        "posts": [],
    }
    base.update(kw)
    return base


POST = {
    "notion_row_id": "n1", "url": "https://www.instagram.com/reel/ABC/",
    "shortcode": "ABC", "media_kind": "릴스", "permalink": "https://www.instagram.com/reel/ABC/",
    "posted_at": "2026-07-18T10:30:00+00:00",
    "thumbnail": "https://cdn/x.jpg", "frozen": False, "days_since_post": 5,
    "metrics": {"views": 12000, "likes": 800, "comments": 55},
    "metrics_updated_at": NOW.isoformat(),
    "history": [{"d": "2026-07-21", "views": 8000, "likes": 500, "comments": 30},
                {"d": "2026-07-22", "views": 10000, "likes": 650, "comments": 40},
                {"d": "2026-07-23", "views": 12000, "likes": 800, "comments": 55}],
    "baseline": {"views_median": 6000, "eng_median": 500, "n": 8},
    "computed": {"er": 0.045, "vs_baseline": 2.0, "cost_per_eng": 456.1, "cost_per_view": 32.5},
    "analysis": {"one_liner": "평소 대비 2배로 순항 중"},
    "last_written_reaction": "x",
}


def _flags(**kw):
    f = {"overdue": [], "unresolvable": [], "unavailable": [], "non_instagram": []}
    f.update(kw)
    return f


def test_render_normal_row():
    html = render_html([_row(posts=[dict(POST)])], _flags(), None, NOW)
    assert "@tester" in html
    assert "평소대비" in html and "2.0x" in html
    assert "4.5%" in html               # ER
    assert "polyline" not in html       # 추이 그래프 제거됨
    assert "2026-07-18" in html         # 게시날짜 (UTC 10:30 → KST 같은 날)
    assert "라세린" in html


def test_render_frozen_and_final():
    p = dict(POST, frozen=True,
             analysis={"one_liner": "x", "final_verdict": "비용 대비 효율적이었다"})
    html = render_html([_row(posts=[p])], _flags(), None, NOW)
    assert "확정" in html
    assert "비용 대비 효율적이었다" in html


def test_render_flag_panel_and_digest():
    overdue = _row(page_id="p2", row_title_raw="no_upload",
                   flags={"overdue_no_output": True, "unresolvable_username": False,
                          "non_instagram": False, "account_unavailable": False})
    bad_name = _row(page_id="p3", row_title_raw="둥둥피치", username=None,
                    flags={"overdue_no_output": False, "unresolvable_username": True,
                           "non_instagram": False, "account_unavailable": False})
    digest = {"headline": "오늘 총평", "notes": ["포인트1", "포인트2"]}
    html = render_html([overdue, bad_name],
                       _flags(overdue=[overdue], unresolvable=[bad_name]), digest, NOW)
    assert "업로드 누락" in html or "결과물 URL 없음" in html
    assert "둥둥피치" in html
    assert "오늘 총평" in html
    # username 인식 실패 행은 브랜드 카드로는 안 나옴
    assert "@None" not in html


def test_render_no_baseline_and_no_posts():
    p = dict(POST, baseline=None,
             computed={"er": 0.045, "vs_baseline": None, "cost_per_eng": 456.1,
                       "cost_per_view": None})
    empty = _row(page_id="p4", row_title_raw="empty_row", username="empty_row", posts=[])
    html = render_html([_row(posts=[p]), empty], _flags(), None, NOW)
    assert "기준치 부족" in html
    assert "@empty_row" not in html  # 결과물 URL 없는 행은 카드 미노출


def test_render_aggregates():
    html = render_html([_row(posts=[dict(POST)])], _flags(), None, NOW)
    assert "브랜드별 집계" in html
    assert "담당자별 집계" in html
    assert "서채윤" in html
    assert "비용 효율 랭킹" in html
    assert "총 참여" in html
