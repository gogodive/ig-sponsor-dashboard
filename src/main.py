"""엔트리포인트: 노션 협찬 DB → 결과물 URL → Apify 수집 → 지표·분석 → 노션 기입 → 대시보드.

사용:
  python -m src.main                  # 전체 파이프라인
  python -m src.main --dry-run        # 노션 기입 생략 (수집·분석·렌더만)
  python -m src.main --only user1,user2
  python -m src.main --skip-analysis  # Claude 호출 생략
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src import analysis as az
from src import metrics as mx
from src.apify_client import fetch_account, fetch_followers, fetch_posts_by_urls
from src.merge import merge_sponsored_post
from src.notion_source import fetch_output_rows, fetch_sponsor_rows, find_output_db_id
from src.notion_write import update_output_row, update_row_score
from src.render import render_html
from src.usernames import extract_shortcode, normalize_username, parse_follower_count

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
SPON_DIR = ROOT / "data" / "sponsorships"
ACC_DIR = ROOT / "data" / "accounts"

log = logging.getLogger("main")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(f: Path) -> dict:
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("손상된 데이터 파일 무시: %s", f)
    return {}


def _save_json(f: Path, data: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _page_key(page_id: str) -> str:
    return page_id.replace("-", "")


def build_row_states(notion_rows: list[dict], cfg: dict, now: datetime) -> list[dict]:
    """노션 행 + 저장분 → 작업 상태 목록. 결과물 DB도 여기서 읽는다 (필요한 행만)."""
    version = cfg["notion"]["version"]
    states = []
    for nrow in notion_rows:
        stored = _load_json(SPON_DIR / f"{_page_key(nrow['page_id'])}.json")
        state = {**stored, **nrow}
        state["username"] = normalize_username(nrow["row_title_raw"])
        state["followers_notion"] = parse_follower_count(nrow["followers_notion_raw"])
        state.setdefault("posts", [])
        state["flags"] = {
            "unresolvable_username": state["username"] is None,
            "non_instagram": "인스타" not in (nrow["media"] or ["인스타"]),
            "overdue_no_output": False,
            "account_unavailable": stored.get("flags", {}).get("account_unavailable", False),
        }
        if state["flags"]["non_instagram"] or state["flags"]["unresolvable_username"]:
            states.append(state)
            continue

        # 종료 + 전부 동결 + 지표 있음 → 노션 하위 DB·Apify 재조회 생략 (비용 고정)
        all_done = (nrow["status"] == "종료" and state["posts"]
                    and all(p.get("frozen") and p.get("metrics_updated_at")
                            for p in state["posts"]))
        state["_settled"] = bool(all_done)
        if all_done:
            states.append(state)
            continue

        if not state.get("child_db_id"):
            state["child_db_id"] = find_output_db_id(nrow["page_id"], version)
        output_rows = fetch_output_rows(state["child_db_id"], version) if state["child_db_id"] else []

        # 결과물 행(URL 있는 것)을 posts 뼈대로 병합 — 저장분과 notion_row_id 로 매칭
        by_row_id = {p.get("notion_row_id"): p for p in state["posts"]}
        posts = []
        url_count = 0
        for orow in output_rows:
            sc = extract_shortcode(orow["url"])
            if orow["url"]:
                url_count += 1
            if not sc:
                continue  # URL 없음(미업로드) 또는 인스타 게시물 URL 아님
            old = by_row_id.get(orow["notion_row_id"], {})
            posts.append({**old, "notion_row_id": orow["notion_row_id"],
                          "url": orow["url"], "shortcode": sc,
                          "media_kind": orow["media_kind"] or "피드",
                          "upload_date_notion": orow["upload_date_notion"]})
        # 결과물 DB 접근 불가 등으로 이번에 못 읽었으면 저장분 유지
        state["posts"] = posts if output_rows else state["posts"]
        state["flags"]["overdue_no_output"] = mx.overdue_flag(
            nrow["status"], nrow["delivery_date"], url_count,
            now.date(), cfg["flags"]["overdue_days"])
        states.append(state)
    return states


def collect_accounts(states: list[dict], cfg: dict, now: datetime) -> dict[str, dict]:
    """고유 계정별 Apify 수집. 반환: username → {followers_count, recent_posts, ok}"""
    need: dict[str, set[str]] = {}
    for s in states:
        if s.get("_settled") or not s.get("username"):
            continue
        pending = [p for p in s["posts"]
                   if not (p.get("frozen") and p.get("metrics_updated_at"))]
        if pending or not s["posts"]:
            # 게시물이 아직 없어도(업로드 대기) 팔로워 최신화를 위해 수집하진 않는다 —
            # 게시물이 하나라도 있는 계정만 Apify 호출해 비용을 아낀다.
            if pending:
                need.setdefault(s["username"], set()).update(
                    p["shortcode"] for p in pending)

    accounts: dict[str, dict] = {}
    actor = cfg["apify"]["actor"]
    for username, shortcodes in need.items():
        acc_stored = _load_json(ACC_DIR / f"{username}.json")
        try:
            snap = fetch_account(username, actor, cfg["apify"]["results_type"],
                                 cfg["apify"]["recent_limit"])
            followers = snap["followers_count"]
            if not followers:
                try:
                    followers = fetch_followers(username, actor)
                except Exception as e:  # noqa: BLE001
                    log.warning("팔로워 조회 실패 @%s: %s", username, str(e).splitlines()[0])
            accounts[username] = {
                "username": username,
                "followers_count": followers or acc_stored.get("followers_count"),
                "recent_posts": snap["posts"],
                "fetched_at": now.isoformat(),
                "ok": True,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("수집 실패 @%s: %s — 저장분 유지", username, str(e).splitlines()[0])
            accounts[username] = {**acc_stored, "username": username,
                                  "recent_posts": acc_stored.get("recent_posts", []),
                                  "ok": False}

    # 최근 창에 없는 협찬 게시물 → 전 계정 모아 한 번에 직접 조회
    missing_urls: list[str] = []
    url_by_shortcode: dict[str, str] = {}
    for s in states:
        u = s.get("username")
        if not u or u not in accounts or not accounts[u]["ok"]:
            continue
        have = {p["post_id"] for p in accounts[u]["recent_posts"]}
        for p in s["posts"]:
            if (p["shortcode"] not in have
                    and not (p.get("frozen") and p.get("metrics_updated_at"))):
                url_by_shortcode[p["shortcode"]] = p["url"]
    missing_urls = list(dict.fromkeys(url_by_shortcode.values()))
    if missing_urls:
        log.info("최근 창 밖 협찬 게시물 %d개 직접 조회", len(missing_urls))
        try:
            direct = fetch_posts_by_urls(missing_urls, actor)
            by_owner: dict[str, list[dict]] = {}
            for p in direct:
                owner = (p.get("owner_username") or "").lower()
                by_owner.setdefault(owner, []).append(p)
            for u, acc in accounts.items():
                extra = by_owner.get(u, [])
                # 소유자 미확인 게시물은 shortcode 로 역매칭
                extra += [p for o, ps in by_owner.items() if not o for p in ps
                          if p["post_id"] in {sp["shortcode"] for s2 in states
                                              if s2.get("username") == u
                                              for sp in s2["posts"]}]
                acc["recent_posts"] = acc["recent_posts"] + extra
        except Exception as e:  # noqa: BLE001
            log.warning("직접 조회 실패: %s", str(e).splitlines()[0])

    for u, acc in accounts.items():
        _save_json(ACC_DIR / f"{u}.json", acc)
    return accounts


def finalize_row(state: dict, accounts: dict[str, dict], cfg: dict, now: datetime,
                 dry_run: bool, skip_analysis: bool) -> dict:
    """행 하나: 병합 → 지표 → 분석 → 노션 기입 → 저장."""
    username = state.get("username")
    acc = accounts.get(username) if username else None
    if acc is not None and not acc.get("ok"):
        state["flags"]["account_unavailable"] = True
    elif acc is not None:
        state["flags"]["account_unavailable"] = False
    if acc:
        state["followers"] = acc.get("followers_count") or state.get("followers_notion")
    else:
        state["followers"] = state.get("followers") or state.get("followers_notion")

    fresh_by_sc = {p["post_id"]: p for p in (acc or {}).get("recent_posts", [])}
    all_sponsored = {p["shortcode"] for p in state["posts"]}

    merged_posts = []
    for post in state["posts"]:
        fresh = fresh_by_sc.get(post["shortcode"])
        baseline = None
        if acc and acc.get("ok"):
            baseline = mx.compute_baseline(
                acc["recent_posts"], all_sponsored, post.get("media_kind") or "피드",
                cfg["baseline"]["max_posts"], cfg["baseline"]["min_posts"])
        p = merge_sponsored_post(post, fresh, baseline, now, cfg["freeze_days"])
        p["computed"] = {
            "er": mx.engagement_rate(p.get("metrics", {}), state["followers"]),
            "vs_baseline": mx.vs_baseline(p, p.get("baseline")),
            "cost_per_eng": mx.cost_per_engagement(state.get("product_value_krw"),
                                                   p.get("metrics", {})),
            "cost_per_view": mx.cost_per_view(state.get("product_value_krw"),
                                              p.get("metrics", {})),
        }
        merged_posts.append(p)
    state["posts"] = merged_posts

    # 분석 (게시물당 최초 1회 + 동결 전환 시 1회)
    if not skip_analysis:
        for p in state["posts"]:
            if not p.get("metrics_updated_at"):
                continue
            a = p.get("analysis", {})
            if not a.get("one_liner"):
                out = az.analyze_post_first(state, p, cfg["claude"], now)
                if out:
                    p["analysis"] = {**a, **out}
            if p.get("frozen") and not p.get("analysis", {}).get("final_verdict"):
                out = az.analyze_post_final(state, p, cfg["claude"], now)
                if out:
                    p["analysis"] = {**p.get("analysis", {}), **out}

    # 노션 기입 (멱등: 직전 기록과 같으면 생략)
    if not dry_run:
        version = cfg["notion"]["version"]
        for p in state["posts"]:
            if not p.get("metrics_updated_at"):
                continue
            reaction = mx.reaction_text(p)
            if reaction != p.get("last_written_reaction"):
                if update_output_row(p["notion_row_id"], reaction,
                                     now.strftime("%Y-%m-%d"), version):
                    p["last_written_reaction"] = reaction
                    log.info("노션 반응도 기입 @%s %s: %s", username, p["shortcode"], reaction)
        score = mx.row_score(state["posts"])
        if score is not None and score != state.get("last_written_score"):
            if update_row_score(state["page_id"], score, version):
                state["last_written_score"] = score
                log.info("노션 메인 반응도 기입 @%s: %d", username, score)

    _save_json(SPON_DIR / f"{_page_key(state['page_id'])}.json",
               {k: v for k, v in state.items() if not k.startswith("_")})
    n_collected = sum(1 for p in state["posts"] if p.get("metrics_updated_at"))
    log.info("%s(@%s): 게시물 %d개 중 지표 %d개 · 상태 %s%s",
             state["row_title_raw"], username, len(state["posts"]), n_collected,
             state["status"],
             " · ⚠️업로드누락" if state["flags"]["overdue_no_output"] else "")
    return state


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="노션 기입 생략")
    ap.add_argument("--only", default=None, help="특정 username 만 (콤마 구분)")
    ap.add_argument("--skip-analysis", action="store_true", help="Claude 분석 생략")
    args = ap.parse_args()

    for key in ("NOTION_TOKEN", "ANTHROPIC_API_KEY", "APIFY_TOKEN"):
        if not os.environ.get(key):
            print(f"{key} 환경변수가 없습니다", file=sys.stderr)
            return 1

    cfg = load_config(ROOT / "config.yaml")
    now = datetime.now(KST)

    notion_rows = fetch_sponsor_rows(cfg["notion"]["sponsor_db_id"], cfg["notion"]["version"])
    log.info("협찬 행 %d개 (진행 중·종료)", len(notion_rows))
    states = build_row_states(notion_rows, cfg, now)

    if args.only:
        wanted = {u.strip().lower() for u in args.only.split(",")}
        work = [s for s in states if s.get("username") in wanted]
    else:
        work = states
    log.info("이번 실행 대상 %d행 (전체 %d행)", len(work), len(states))

    accounts = collect_accounts(work, cfg, now)
    done_by_page = {}
    for s in work:
        if s["flags"]["non_instagram"] or s["flags"]["unresolvable_username"]:
            _save_json(SPON_DIR / f"{_page_key(s['page_id'])}.json",
                       {k: v for k, v in s.items() if not k.startswith("_")})
            done_by_page[s["page_id"]] = s
        elif s.get("_settled"):
            done_by_page[s["page_id"]] = s
        else:
            done_by_page[s["page_id"]] = finalize_row(
                s, accounts, cfg, now, args.dry_run, args.skip_analysis)

    # --only 로 일부만 처리해도 대시보드는 항상 전체 행으로 렌더
    all_rows = [done_by_page.get(s["page_id"], s) for s in states]

    flags = {
        "overdue": [s for s in all_rows if s["flags"].get("overdue_no_output")],
        "unresolvable": [s for s in all_rows if s["flags"].get("unresolvable_username")],
        "unavailable": [s for s in all_rows if s["flags"].get("account_unavailable")],
        "non_instagram": [s for s in all_rows if s["flags"].get("non_instagram")],
    }
    digest = None
    if not args.skip_analysis:
        digest = az.daily_digest(all_rows, flags, cfg["claude"], now)

    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text(
        render_html(all_rows, flags, digest, now), encoding="utf-8")
    print(f"완료: 협찬 {len(all_rows)}행 → site/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
