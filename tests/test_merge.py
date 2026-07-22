from datetime import datetime, timedelta, timezone

from src.merge import is_frozen, merge_sponsored_post, post_age_days

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 23, 8, 0, tzinfo=KST)


def _fresh(views=1000, likes=100, comments=10, posted_days_ago=5):
    posted = NOW - timedelta(days=posted_days_ago)
    return {
        "post_id": "ABC123",
        "posted_at": posted.isoformat(),
        "permalink": "https://www.instagram.com/reel/ABC123/",
        "thumbnail": "https://cdn/x.jpg",
        "caption": "cap",
        "media_type": "VIDEO",
        "product": "REELS",
        "owner_username": "tester",
        "metrics": {"views": views, "likes": likes, "comments": comments},
    }


def _stored():
    return {"notion_row_id": "nid", "url": "https://www.instagram.com/reel/ABC123/",
            "shortcode": "ABC123", "media_kind": "릴스", "upload_date_notion": None}


def test_live_post_updates_and_history():
    p = merge_sponsored_post(_stored(), _fresh(), {"views_median": 500, "eng_median": 50, "n": 5}, NOW)
    assert p["frozen"] is False
    assert p["metrics"]["views"] == 1000
    assert len(p["history"]) == 1
    assert p["history"][0]["d"] == "2026-07-23"
    assert p["baseline"]["views_median"] == 500
    assert p["days_since_post"] == 5


def test_same_day_rerun_idempotent():
    p1 = merge_sponsored_post(_stored(), _fresh(views=1000), None, NOW)
    p2 = merge_sponsored_post(p1, _fresh(views=1100), None, NOW)
    assert len(p2["history"]) == 1          # 같은 날짜는 교체
    assert p2["history"][0]["views"] == 1100


def test_none_never_overwrites():
    p1 = merge_sponsored_post(_stored(), _fresh(views=1000), None, NOW)
    fresh2 = _fresh(views=None, likes=120)
    p2 = merge_sponsored_post(p1, fresh2, None, NOW + timedelta(days=1))
    assert p2["metrics"]["views"] == 1000   # None 으로 덮지 않음
    assert p2["metrics"]["likes"] == 120


def test_freeze_after_30_days():
    old = _fresh(posted_days_ago=31)
    p1 = merge_sponsored_post(_stored(), old, None, NOW)
    assert p1["frozen"] is True
    assert p1["metrics"]["views"] == 1000   # 지표 없던 상태 → 최초 1회 백필
    # 동결 후 재수집돼도 지표 불변, history 추가 없음
    p2 = merge_sponsored_post(p1, _fresh(views=9999, posted_days_ago=31), None, NOW)
    assert p2["metrics"]["views"] == 1000
    assert not p2.get("history")


def test_baseline_frozen_with_post():
    live = merge_sponsored_post(_stored(), _fresh(posted_days_ago=29),
                                {"views_median": 500, "eng_median": 50, "n": 5}, NOW)
    later = NOW + timedelta(days=5)
    frozen = merge_sponsored_post(live, _fresh(posted_days_ago=34),
                                  {"views_median": 900, "eng_median": 90, "n": 5}, later)
    assert frozen["frozen"] is True
    assert frozen["baseline"]["views_median"] == 500  # 동결 후 갱신 안 됨


def test_fetch_failure_keeps_stored():
    p1 = merge_sponsored_post(_stored(), _fresh(), None, NOW)
    p2 = merge_sponsored_post(p1, None, None, NOW + timedelta(days=1))
    assert p2["metrics"]["views"] == 1000
    assert len(p2["history"]) == 1


def test_age_fallback_to_notion_upload_date():
    stored = dict(_stored(), upload_date_notion="2026-06-01")
    assert post_age_days(stored, NOW) == 52
    assert is_frozen(stored, NOW) is True
    assert post_age_days(_stored(), NOW) is None
