"""Claude 분석: ①게시물 최초 수집 시 한줄 평가 ②동결 시 최종 평가 ③일일 다이제스트.

게시물당 ①②는 각각 평생 1회만 호출하고 결과를 캐시한다 (토큰 절약).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests

log = logging.getLogger(__name__)

_SYSTEM = (
    "너는 인플루언서 마케팅 성과 분석가다. 우리 회사(다이빙·수영 브랜드)가 제품을 협찬한 "
    "인플루언서의 인스타 게시물 성과를 평가한다. 공개 지표는 조회수(릴스)·좋아요·댓글뿐이다. "
    "'평소대비'는 그 계정의 협찬 아닌 최근 게시물 중앙값 대비 배수다. "
    "반드시 지정된 JSON 하나만 출력하고 다른 텍스트는 쓰지 마라."
)


def _call(system: str, user: str, model: str, max_tokens: int) -> dict:
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=180,
    )
    res.raise_for_status()
    text = "".join(b.get("text", "") for b in res.json().get("content", [])
                   if b.get("type") == "text")
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def _post_context(row: dict, post: dict) -> str:
    m = post.get("metrics", {})
    c = post.get("computed", {})
    b = post.get("baseline") or {}
    lines = [
        f"# 협찬: @{row.get('username')} · 브랜드 {'/'.join(row.get('brands', []))} · "
        f"상품 가액 {row.get('product_value_krw') or '?'}원 · 팔로워 {row.get('followers') or '?'}",
        f"# 게시물 [{post.get('media_kind')}] D+{post.get('days_since_post')}",
        f"조회 {m.get('views')} · 좋아요 {m.get('likes')} · 댓글 {m.get('comments')}",
    ]
    if c.get("vs_baseline") is not None:
        lines.append(f"평소대비 {c['vs_baseline']:.2f}x (베이스라인 n={b.get('n')})")
    if c.get("er") is not None:
        lines.append(f"팔로워 대비 참여율 {c['er'] * 100:.2f}%")
    if c.get("cost_per_eng") is not None:
        lines.append(f"참여 1건당 비용 {c['cost_per_eng']:.0f}원")
    if c.get("cost_per_view") is not None:
        lines.append(f"조회 1회당 비용 {c['cost_per_view']:.1f}원")
    return "\n".join(lines)


def analyze_post_first(row: dict, post: dict, cfg: dict, now: datetime) -> dict | None:
    """최초 수집 시 한줄 평가 → {"one_liner", "analyzed_at"}"""
    user = (
        f"{_post_context(row, post)}\n\n"
        '# 출력: {"one_liner": "초기 성과 평가 한 문장 (80자 이내, 평소대비·비용 관점)"}'
    )
    try:
        out = _call(_SYSTEM, user, cfg["model"], cfg["max_tokens_post"])
        return {"one_liner": str(out.get("one_liner", ""))[:200], "analyzed_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        log.warning("한줄 평가 실패 %s: %s", post.get("shortcode"), e)
        return None


def analyze_post_final(row: dict, post: dict, cfg: dict, now: datetime) -> dict | None:
    """동결 시 최종 평가 → {"final_verdict", "final_at"}"""
    user = (
        f"{_post_context(row, post)}\n"
        f"# 30일 추적이 끝나 지표가 확정됐다.\n\n"
        '# 출력: {"final_verdict": "이 협찬이 비용 대비 가치 있었는지 성과·비용 관점 2~3문장"}'
    )
    try:
        out = _call(_SYSTEM, user, cfg["model"], cfg["max_tokens_final"])
        return {"final_verdict": str(out.get("final_verdict", ""))[:600],
                "final_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        log.warning("최종 평가 실패 %s: %s", post.get("shortcode"), e)
        return None


def daily_digest(rows: list[dict], flags: dict, cfg: dict, now: datetime) -> dict | None:
    """대시보드 헤더용 일일 다이제스트 → {"headline", "notes":[...]}"""
    live = [(r, p) for r in rows for p in r.get("posts", [])
            if not p.get("frozen") and p.get("metrics")]
    if not live and not any(flags.values()):
        return None
    lines = []
    for r, p in sorted(live, key=lambda x: -(x[1].get("computed", {}).get("vs_baseline") or 0))[:15]:
        c = p.get("computed", {})
        vs = f"{c['vs_baseline']:.1f}x" if c.get("vs_baseline") is not None else "?"
        lines.append(f"@{r.get('username')} [{p.get('media_kind')}] D+{p.get('days_since_post')} "
                     f"평소대비 {vs} · {'/'.join(r.get('brands', []))} · 담당 {r.get('manager')}")
    flag_txt = (f"업로드 누락 {len(flags.get('overdue', []))}건 · "
                f"계정 확인 필요 {len(flags.get('unresolvable', []))}건")
    user = (
        f"# 오늘({now.strftime('%Y-%m-%d')}) 추적 중인 협찬 게시물 (평소대비 순)\n"
        + "\n".join(lines) + f"\n# 플래그: {flag_txt}\n\n"
        '# 출력: {"headline": "오늘의 협찬 성과 총평 한 문장", '
        '"notes": ["마케팅팀이 챙겨야 할 포인트 2~4개 (잘된 것/못된 것/조치 필요)"]}'
    )
    try:
        out = _call(_SYSTEM, user, cfg["model"], cfg["max_tokens_daily"])
        return {"headline": str(out.get("headline", ""))[:300],
                "notes": [str(x)[:300] for x in out.get("notes", [])][:4],
                "generated_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        log.warning("일일 다이제스트 실패: %s", e)
        return None
