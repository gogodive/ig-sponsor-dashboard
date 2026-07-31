"""[일회성 도구] 인스타 리뷰어 이벤트 댓글 → 신청자 지표 수집 → 상위 N명 선발 → 노션 기록.

매일 자동화(src/main.py)와 무관하게 수동 실행 전용.

사용:
  python -m tools.reviewer_pick --post-url "https://www.instagram.com/p/XXXX/" \
      --notion-page 3ab39eba97ed806b9ee9d6e4a931e0a7 --top 20
  옵션: --limit-comments 1000 --max-profiles 50(테스트용 표본) --dry-run(노션 기록 생략)

선발 로직: 비공개·휴면(90일)·팔로워 300 미만 제외 후
  종합점수 = 참여율 45% + 팔로워 규모 30% + 활동 빈도 25% (각 백분위 환산)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src.apify_client import _run_actor
from src.notion_source import API as NOTION_API
from src.notion_source import _headers

KST = timezone(timedelta(hours=9))
CACHE_DIR = Path(__file__).parent.parent / "data" / "reviewer"
ACTOR = "apify~instagram-scraper"
# 기본 댓글 수집기: 답글(대댓글)까지 가져온다. 예시 댓글에 답글로 응모하는 사람이 많아
# 최상위 댓글만 받으면 대량 누락된다 (실측: 173명 → 212명).
COMMENT_ACTOR = "apify~instagram-comment-scraper"
_MENTION = re.compile(r"@[A-Za-z0-9._]{2,}")
_BRACKET = re.compile(r"\[[^\]]{1,30}\]")
PROFILE_CHUNK = 100          # 프로필 조회 1회당 계정 수
DORMANT_DAYS = 90
MIN_FOLLOWERS = 300
WEIGHTS = {"er": 0.45, "followers": 0.30, "activity": 0.25}

log = logging.getLogger("reviewer_pick")


# ── 수집 ──────────────────────────────────────────────────────────────────
def fetch_comments(post_url: str, limit: int, diag: bool = False,
                   actor: str = ACTOR) -> list[dict]:
    """게시물 댓글 수집 → [{username, text, at}].

    기본 actor(instagram-scraper)는 최상위 댓글만 준다. 답글로 응모한 사람까지
    잡으려면 comment-scraper actor 를 쓴다 (includeNestedComments).
    """
    if "comment-scraper" in actor:
        payload = {"directUrls": [post_url], "resultsLimit": limit,
                   "includeNestedComments": True}
    else:
        payload = {"directUrls": [post_url], "resultsType": "comments",
                   "resultsLimit": limit, "addParentData": False}
    items = _run_actor(actor, payload)
    if diag:
        _diagnose(items)
        dump = cache_path(post_url).with_name(cache_path(post_url).stem + "_comments.json")
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps(
            [{"u": it.get("ownerUsername"), "t": (it.get("text") or "")[:300],
              "at": it.get("timestamp"), "id": it.get("id")} for it in items],
            ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("[진단] 댓글 원문 저장: %s", dump)
    out = []
    for it in items:
        user = (it.get("ownerUsername") or it.get("username")
                or (it.get("owner") or {}).get("username"))
        if not user:
            continue
        out.append({"username": user.lower().strip(),
                    "text": (it.get("text") or "")[:200],
                    "at": it.get("timestamp")})
    return out


def _diagnose(items: list[dict]) -> None:
    """수집 댓글 구조 진단 — 인스타 표기 댓글 수와 차이가 날 때 원인 파악용."""
    log.info("[진단] 수집 항목 %d개", len(items))
    if not items:
        return
    log.info("[진단] 항목 필드: %s", sorted(items[0].keys()))
    replies = sum(int(it.get("repliesCount") or 0) for it in items)
    nested = sum(len(it.get("replies") or []) for it in items
                 if isinstance(it.get("replies"), list))
    owners: dict[str, int] = {}
    for it in items:
        u = (it.get("ownerUsername") or it.get("username")
             or (it.get("owner") or {}).get("username") or "?")
        owners[u] = owners.get(u, 0) + 1
    top = sorted(owners.items(), key=lambda x: -x[1])[:5]
    log.info("[진단] repliesCount 합계 %d · 항목에 포함된 replies %d개", replies, nested)
    log.info("[진단] 고유 작성자 %d명 · 다중 댓글 상위: %s", len(owners), top)
    log.info("[진단] 예시 항목: %s",
             json.dumps({k: v for k, v in items[0].items()
                         if k in ("id", "text", "ownerUsername", "timestamp",
                                  "repliesCount", "parentId", "likesCount")},
                        ensure_ascii=False)[:400])


def is_entry(texts: list[str], min_text: int) -> bool:
    """응모 댓글로 볼지 판정 — 친구 태그 / 대괄호 양식 / 최소 길이 중 하나 충족.

    실제 응모는 양식을 안 지킨 경우가 대부분이라(대괄호 사용 32/211) 관대하게 잡고,
    단순 감탄사·문의 댓글만 걸러낸다.
    """
    joined = " ".join(texts)
    return bool(_MENTION.search(joined) or _BRACKET.search(joined)
                or max((len(t.strip()) for t in texts), default=0) >= min_text)


def cache_path(post_url: str) -> Path:
    """게시물 shortcode 기준 캐시 파일 경로."""
    m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", post_url)
    return CACHE_DIR / f"{m.group(1) if m else 'event'}.json"


def load_cache(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """(profiles, applicants) 반환. applicants 는 누적 신청자 명부."""
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return d.get("profiles", {}), d.get("applicants", {})
        except json.JSONDecodeError:
            log.warning("손상된 캐시 무시: %s", path)
    return {}, {}


def merge_applicants(known: dict[str, dict], counts: dict[str, int],
                     texts: dict[str, list[str]], now: datetime) -> dict[str, dict]:
    """이번 수집분을 누적 명부에 합친다.

    인스타 스크래핑은 간헐적으로 일부만 반환한다(실측: 272건 → 178건).
    한 번 확인된 신청자를 유지해야 부분 수집 때문에 탈락하는 일이 없다.
    """
    merged = {u: dict(v) for u, v in known.items()}
    for u, n in counts.items():
        cur = merged.setdefault(u, {"comments": 0, "texts": [],
                                    "first_seen": now.strftime("%Y-%m-%d")})
        cur["comments"] = max(cur.get("comments", 0), n)
        seen = set(cur.get("texts", []))
        cur["texts"] = cur.get("texts", []) + [t for t in texts.get(u, [])
                                               if t and t not in seen]
        cur["last_seen"] = now.strftime("%Y-%m-%d")
    return merged


def slim(prof: dict) -> dict:
    """지표 계산에 필요한 필드만 남긴다 (원본은 계정당 100KB+ 라 캐시가 비대해짐)."""
    posts = prof.get("latestPosts") if isinstance(prof.get("latestPosts"), list) else []
    return {
        "username": prof.get("username"),
        "followersCount": prof.get("followersCount"),
        "postsCount": prof.get("postsCount"),
        "private": bool(prof.get("private") or prof.get("isPrivate")),
        "latestPosts": [{"likesCount": p.get("likesCount"),
                         "commentsCount": p.get("commentsCount"),
                         "timestamp": p.get("timestamp")} for p in posts[:12]],
    }


def save_cache(path: Path, profiles: dict[str, dict], applicants: dict[str, dict],
               now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"updated_at": now.isoformat(),
         "profiles": {u: slim(p) for u, p in profiles.items()},
         "applicants": applicants},
        ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_profiles(usernames: list[str]) -> dict[str, dict]:
    """프로필 일괄 조회 (details). username → raw item"""
    result: dict[str, dict] = {}
    for i in range(0, len(usernames), PROFILE_CHUNK):
        chunk = usernames[i:i + PROFILE_CHUNK]
        log.info("프로필 조회 %d~%d / %d", i + 1, i + len(chunk), len(usernames))
        try:
            items = _run_actor(ACTOR, {
                "directUrls": [f"https://www.instagram.com/{u}/" for u in chunk],
                "resultsType": "details",
                "resultsLimit": 1,
                "addParentData": False,
            })
        except Exception as e:  # noqa: BLE001
            log.warning("프로필 조회 실패(구간 건너뜀): %s", str(e).splitlines()[0])
            continue
        for it in items:
            u = (it.get("username") or "").lower()
            if u:
                result[u] = it
    return result


# ── 지표 계산 ─────────────────────────────────────────────────────────────
def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_metrics(username: str, prof: dict | None, comments: int,
                  now: datetime) -> dict:
    """신청자 1명의 지표 + 제외 사유."""
    row = {"username": username, "comments": comments, "excluded": None,
           "followers": None, "posts": None, "avg_eng": None, "er": None,
           "recent_posts_90d": 0, "last_post": None, "private": None}
    if not prof:
        row["excluded"] = "조회 실패(삭제/오타/차단)"
        return row

    row["followers"] = prof.get("followersCount")
    row["posts"] = prof.get("postsCount")
    row["private"] = bool(prof.get("private") or prof.get("isPrivate"))

    latest = prof.get("latestPosts") if isinstance(prof.get("latestPosts"), list) else []
    engs, dates = [], []
    for p in latest[:12]:
        likes, cmts = p.get("likesCount"), p.get("commentsCount")
        if isinstance(likes, int) and likes >= 0:
            engs.append(likes + (cmts if isinstance(cmts, int) else 0))
        d = _parse_ts(p.get("timestamp"))
        if d:
            dates.append(d)
    if engs:
        row["avg_eng"] = round(statistics.mean(engs))
    if dates:
        last = max(dates)
        row["last_post"] = last.astimezone(KST).strftime("%Y-%m-%d")
        row["recent_posts_90d"] = sum(
            1 for d in dates if (now - d.astimezone(now.tzinfo)).days <= DORMANT_DAYS)
    if row["avg_eng"] and row["followers"]:
        row["er"] = row["avg_eng"] / row["followers"]

    # 제외 판정
    if row["private"]:
        row["excluded"] = "비공개 계정"
    elif row["followers"] is None:
        row["excluded"] = "지표 수집 실패"
    elif row["followers"] < MIN_FOLLOWERS:
        row["excluded"] = f"팔로워 {MIN_FOLLOWERS}명 미만"
    elif row["recent_posts_90d"] == 0:
        row["excluded"] = f"휴면({DORMANT_DAYS}일간 게시물 없음)"
    return row


def _pct_rank(values: list[float], v: float) -> float:
    """백분위(0~1). 동점은 같은 값."""
    if not values:
        return 0.0
    below = sum(1 for x in values if x < v)
    same = sum(1 for x in values if x == v)
    return (below + same / 2) / len(values)


def score_rows(rows: list[dict]) -> list[dict]:
    """유효 신청자에 종합 점수(0~100) 부여."""
    valid = [r for r in rows if not r["excluded"]]
    ers = [r["er"] for r in valid if r["er"] is not None]
    fols = [r["followers"] for r in valid if r["followers"]]
    acts = [r["recent_posts_90d"] for r in valid]
    for r in valid:
        p_er = _pct_rank(ers, r["er"]) if r["er"] is not None else 0.0
        p_fol = _pct_rank(fols, r["followers"]) if r["followers"] else 0.0
        p_act = _pct_rank(acts, r["recent_posts_90d"])
        r["score"] = round(100 * (WEIGHTS["er"] * p_er + WEIGHTS["followers"] * p_fol
                                  + WEIGHTS["activity"] * p_act), 1)
    valid.sort(key=lambda r: -r["score"])
    for i, r in enumerate(valid, 1):
        r["rank"] = i
    return valid


# ── 노션 기록 ─────────────────────────────────────────────────────────────
def _rt(text: str, link: str | None = None) -> list[dict]:
    t = {"type": "text", "text": {"content": str(text)[:1900]}}
    if link:
        t["text"]["link"] = {"url": link}
    return [t]


def _cells(values: list) -> dict:
    return {"object": "block", "type": "table_row",
            "table_row": {"cells": [_rt(v if v is not None else "–") for v in values]}}


def _fmt(r: dict) -> list:
    return [
        r.get("rank", "–"),
        f"@{r['username']}",
        f"{r['followers']:,}" if r.get("followers") else "–",
        f"{r['avg_eng']:,}" if r.get("avg_eng") else "–",
        f"{r['er'] * 100:.1f}%" if r.get("er") is not None else "–",
        f"{r.get('recent_posts_90d', 0)}개",
        r.get("last_post") or "–",
        f"{r.get('score', '–')}",
    ]


HEADER = ["#", "계정", "팔로워", "평균 참여", "ER", "90일 게시", "최근 게시", "점수"]


def _table_block(rows: list[dict], limit: int = 95) -> dict:
    children = [_cells(HEADER)] + [_cells(_fmt(r)) for r in rows[:limit]]
    return {"object": "block", "type": "table",
            "table": {"table_width": len(HEADER), "has_column_header": True,
                      "has_row_header": False, "children": children}}


def _append(page_id: str, blocks: list[dict], version: str) -> list[dict]:
    res = requests.patch(f"{NOTION_API}/blocks/{page_id}/children",
                         headers=_headers(version), json={"children": blocks}, timeout=60)
    if not res.ok:
        log.warning("노션 기록 실패: %s", res.text[:300])
        return []
    return res.json().get("results", [])


def clear_page(page_id: str, version: str) -> None:
    """기존 블록 전부 보관 처리 (재실행 시 중복 방지)."""
    res = requests.get(f"{NOTION_API}/blocks/{page_id}/children?page_size=100",
                       headers=_headers(version), timeout=60)
    if not res.ok:
        return
    for b in res.json().get("results", []):
        requests.delete(f"{NOTION_API}/blocks/{b['id']}", headers=_headers(version),
                        timeout=60)


def write_notion(page_id: str, post_url: str, picked: list[dict], ranked: list[dict],
                 excluded: list[dict], now: datetime, top: int, waiting_n: int = 30,
                 version: str = "2022-06-28") -> None:
    clear_page(page_id, version)
    head = [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": _rt(f"{now.strftime('%Y-%m-%d %H:%M')} KST 집계 · "
                             f"신청 {len(ranked) + len(excluded)}명 → 유효 {len(ranked)}명 · "
                             f"선발 {len(picked)}명 (제외 {len(excluded)}명)"),
            "icon": {"emoji": "🏅"}, "color": "gray_background"}},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": _rt("이벤트 게시물 바로가기", post_url)}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(
            "선발 기준: 비공개·휴면(90일 무게시)·팔로워 300명 미만 제외 후 "
            "종합점수 = 참여율(ER) 45% + 팔로워 규모 30% + 활동 빈도 25% (각 백분위 환산). "
            "ER = 최근 게시물 평균 참여(좋아요+댓글) ÷ 팔로워.")}},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": _rt(f"🏅 선발 {len(picked)}명")}},
    ]
    _append(page_id, head, version)
    _append(page_id, [_table_block(picked)], version)

    waiting = ranked[top:top + waiting_n]
    if waiting:
        _append(page_id, [{"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": _rt(f"예비 {len(waiting)}명 (차순위)")}}], version)
        _append(page_id, [_table_block(waiting)], version)

    _append(page_id, [{"object": "block", "type": "heading_2", "heading_2": {
        "rich_text": _rt(f"전체 신청자 랭킹 ({len(ranked)}명 · 상위 95명 표시)")}}], version)
    _append(page_id, [_table_block(ranked)], version)

    if excluded:
        by_reason: dict[str, list[str]] = {}
        for r in excluded:
            by_reason.setdefault(r["excluded"], []).append("@" + r["username"])
        blocks = [{"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": _rt(f"제외 {len(excluded)}명")}}]
        for reason, users in sorted(by_reason.items(), key=lambda x: -len(x[1])):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rt(
                               f"{reason} — {len(users)}명: " + ", ".join(users[:60])
                               + (" 외" if len(users) > 60 else ""))}})
        _append(page_id, blocks, version)


# ── 실행 ──────────────────────────────────────────────────────────────────
def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-url", required=True)
    ap.add_argument("--notion-page", default=None, help="결과 기록할 노션 페이지 id")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--waiting", type=int, default=30, help="예비(차순위) 인원")
    ap.add_argument("--limit-comments", type=int, default=1000)
    ap.add_argument("--max-profiles", type=int, default=0, help="0=전체 (테스트 시 표본 수)")
    ap.add_argument("--exclude", default="", help="제외할 계정 (콤마 구분, 예: 자사 계정)")
    ap.add_argument("--no-cache", action="store_true", help="캐시 무시하고 전원 재조회")
    ap.add_argument("--diag", action="store_true", help="댓글 수집 구조만 진단하고 종료")
    ap.add_argument("--comment-actor", default=COMMENT_ACTOR,
                    help="댓글 수집 actor (기본: 답글 포함 comment-scraper)")
    ap.add_argument("--min-text", type=int, default=20,
                    help="응모로 인정할 최소 댓글 길이 (태그·양식 있으면 무관)")
    ap.add_argument("--dry-run", action="store_true", help="노션 기록 생략")
    args = ap.parse_args()

    for key in ("APIFY_TOKEN",) + (() if args.dry_run else ("NOTION_TOKEN",)):
        if not os.environ.get(key):
            print(f"{key} 환경변수가 없습니다", file=sys.stderr)
            return 1

    now = datetime.now(KST)
    comments = fetch_comments(args.post_url, args.limit_comments, diag=args.diag,
                              actor=args.comment_actor)
    if args.diag:
        return 0
    skip = {u.strip().lower().lstrip("@") for u in args.exclude.split(",") if u.strip()}
    counts: dict[str, int] = {}
    texts: dict[str, list[str]] = {}
    for c in comments:
        u = c["username"]
        if u in skip:
            continue
        counts[u] = counts.get(u, 0) + 1
        texts.setdefault(u, []).append(c["text"])
    log.info("이번 수집: 댓글 %d개 → 작성자 %d명", len(comments), len(counts))

    # 누적 명부와 병합 — 부분 수집으로 기존 신청자가 빠지는 것을 방지
    cpath = cache_path(args.post_url)
    cached_profiles, known = ({}, {}) if args.no_cache else load_cache(cpath)
    applicants = merge_applicants(known, counts, texts, now)
    new_this_run = [u for u in counts if u not in known]
    log.info("누적 신청 명부 %d명 (기존 %d · 이번 신규 %d)",
             len(applicants), len(known), len(new_this_run))
    if not applicants:
        print("신청자를 찾지 못했습니다", file=sys.stderr)
        return 1
    counts = {u: v.get("comments", 1) for u, v in applicants.items()}
    texts = {u: v.get("texts", []) for u, v in applicants.items()}

    not_entry = [u for u in counts if not is_entry(texts[u], args.min_text)]
    if not_entry:
        log.info("응모 양식 미충족 %d명 제외: %s", len(not_entry),
                 ", ".join("@" + u for u in not_entry[:20]))
    users = [u for u in counts if u not in set(not_entry)]
    log.info("신청자 %d명", len(users))
    if args.max_profiles:
        users = users[:args.max_profiles]
        log.info("표본 모드: %d명만 조회", len(users))

    # 프로필: 이미 조회한 신청자는 건너뛰고 신규만 조회 (분할 실행 대응)
    cached = dict(cached_profiles)
    todo = [u for u in users if u not in cached]
    log.info("프로필: 캐시 %d명 재사용 · 신규 %d명 조회", len(users) - len(todo), len(todo))
    fresh = fetch_profiles(todo) if todo else {}
    for u in todo:  # 조회 실패는 캐시에 넣지 않아 다음 실행에서 재시도된다
        if u in fresh:
            cached[u] = fresh[u]
    save_cache(cpath, cached, applicants, now)
    profiles = cached

    rows = [build_metrics(u, profiles.get(u), counts[u], now) for u in users]
    rows += [{"username": u, "comments": counts[u], "excluded": "응모 양식 미충족",
              "followers": None, "posts": None, "avg_eng": None, "er": None,
              "recent_posts_90d": 0, "last_post": None, "private": None}
             for u in not_entry]
    ranked = score_rows(rows)
    excluded = [r for r in rows if r["excluded"]]
    picked = ranked[:args.top]

    print(f"\n신청 {len(users)}명 → 유효 {len(ranked)}명 / 제외 {len(excluded)}명")
    print(f"── 선발 {len(picked)}명 ──")
    for r in picked:
        print(f"{r['rank']:>3}. @{r['username']:<24} 팔로워 {r['followers']:>8,} "
              f"· 평균참여 {r['avg_eng'] or 0:>6,} · ER {(r['er'] or 0) * 100:>5.1f}% "
              f"· 점수 {r['score']}")

    if args.notion_page and not args.dry_run:
        write_notion(args.notion_page, args.post_url, picked, ranked, excluded,
                     now, args.top, args.waiting)
        print(f"\n노션 기록 완료: {args.notion_page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
