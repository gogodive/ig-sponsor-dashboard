"""수집·분석 결과 → 단일 HTML 대시보드 (ig-ref-dashboard 렌더러 개조판)."""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Undefined, select_autoescape

KST = timezone(timedelta(hours=9))
_TEMPLATE_DIR = Path(__file__).parent

BRAND_COLORS = {
    "인투더블루": "#1565c0",
    "딥바이브": "#e65100",
    "고고다이브": "#2e7d32",
    "라세린": "#ad1457",
    "시크릿스": "#6d4c41",
    "기타": "#616161",
}
BRAND_ORDER = ["고고다이브", "인투더블루", "딥바이브", "라세린", "시크릿스", "기타"]


def _fmt_num(v) -> str:
    if v is None or isinstance(v, Undefined):
        return "–"
    return f"{v:,}" if isinstance(v, int) else f"{v:,.0f}"


def _fmt_pct(v) -> str:
    if v is None or isinstance(v, Undefined):
        return "–"
    return f"{v * 100:.1f}%"


def _fmt_x(v) -> str:
    if v is None or isinstance(v, Undefined):
        return "–"
    return f"{v:.1f}x"


def _fmt_krw(v) -> str:
    if v is None or isinstance(v, Undefined):
        return "–"
    if v >= 10_000:
        return f"{v / 10_000:,.0f}만원"
    return f"{v:,.0f}원"


def _thumb_proxy(url) -> str:
    """인스타 CDN 은 CORP: same-origin 이라 weserv 이미지 프록시를 경유시킨다."""
    if not url or isinstance(url, Undefined):
        return ""
    return "https://images.weserv.nl/?url=" + urllib.parse.quote(str(url), safe="")


def _sparkline(history: list[dict], key: str) -> str:
    """일별 히스토리 → 인라인 SVG polyline 좌표 문자열. 값 2개 미만이면 ''."""
    vals = [(h["d"], h.get(key)) for h in history if isinstance(h.get(key), (int, float))]
    if len(vals) < 2:
        return ""
    ys = [v for _, v in vals]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1
    w, h_px = 120, 28
    step = w / (len(vals) - 1)
    pts = [f"{i * step:.1f},{h_px - 2 - (v - lo) / span * (h_px - 4):.1f}"
           for i, (_, v) in enumerate(vals)]
    return " ".join(pts)


def _vs_class(v) -> str:
    if v is None:
        return "na"
    if v >= 1.5:
        return "good"
    if v >= 0.7:
        return "mid"
    return "bad"


def _primary_brand(row: dict) -> str:
    brands = row.get("brands") or []
    return brands[0] if brands else "기타"


def _agg(rows: list[dict], key_fn) -> list[dict]:
    """브랜드/담당자별 집계표."""
    buckets: dict[str, dict] = {}
    for r in rows:
        key = key_fn(r) or "미지정"
        b = buckets.setdefault(key, {"key": key, "rows": 0, "posts": 0, "value": 0,
                                     "vs": [], "cpe": [], "flags": 0})
        b["rows"] += 1
        b["value"] += r.get("product_value_krw") or 0
        b["flags"] += 1 if any(r.get("flags", {}).values()) else 0
        for p in r.get("posts", []):
            if not p.get("metrics_updated_at"):
                continue
            b["posts"] += 1
            c = p.get("computed", {})
            if isinstance(c.get("vs_baseline"), (int, float)):
                b["vs"].append(c["vs_baseline"])
            if isinstance(c.get("cost_per_eng"), (int, float)):
                b["cpe"].append(c["cost_per_eng"])
    out = []
    for b in buckets.values():
        b["vs_mean"] = sum(b["vs"]) / len(b["vs"]) if b["vs"] else None
        b["cpe_mean"] = sum(b["cpe"]) / len(b["cpe"]) if b["cpe"] else None
        out.append(b)
    out.sort(key=lambda x: -(x["vs_mean"] or 0))
    return out


def render_html(rows: list[dict], flags: dict, digest: dict | None,
                generated_at: datetime) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["num"] = _fmt_num
    env.filters["pct"] = _fmt_pct
    env.filters["x"] = _fmt_x
    env.filters["krw"] = _fmt_krw
    env.filters["thumb"] = _thumb_proxy
    tpl = env.get_template("template.html")

    visible = [r for r in rows
               if not r.get("flags", {}).get("non_instagram")
               and not r.get("flags", {}).get("unresolvable_username")]
    # 구형 종료 행(결과물 DB 없던 시절 → 수집 게시물 0개)은 카드에서 제외
    visible = [r for r in visible
               if not (r.get("status") == "종료"
                       and not any(p.get("metrics_updated_at")
                                   for p in r.get("posts", [])))]
    for r in visible:
        for p in r.get("posts", []):
            p["_spark_views"] = _sparkline(p.get("history", []), "views")
            p["_spark_likes"] = _sparkline(p.get("history", []), "likes")
            p["_vs_class"] = _vs_class(p.get("computed", {}).get("vs_baseline"))
        r["_last_post"] = max((p.get("posted_at") or p.get("upload_date_notion") or ""
                               for p in r.get("posts", [])), default="")

    by_brand: dict[str, list[dict]] = {}
    for r in visible:
        by_brand.setdefault(_primary_brand(r), []).append(r)
    order = [b for b in BRAND_ORDER if b in by_brand] + \
            [b for b in by_brand if b not in BRAND_ORDER]
    groups = []
    for b in order:
        rs = by_brand[b]
        rs.sort(key=lambda r: (r.get("status") != "진행 중", r["_last_post"]), reverse=False)
        rs.sort(key=lambda r: r["_last_post"], reverse=True)
        rs.sort(key=lambda r: r.get("status") != "진행 중")
        groups.append({"name": b, "color": BRAND_COLORS.get(b, "#616161"), "rows": rs})

    tracked = [p for r in visible for p in r.get("posts", []) if p.get("metrics_updated_at")]
    live = [p for p in tracked if not p.get("frozen")]
    vs_vals = [p["computed"]["vs_baseline"] for p in tracked
               if isinstance(p.get("computed", {}).get("vs_baseline"), (int, float))]
    kpi = {
        "rows": len(visible),
        "live_posts": len(live),
        "frozen_posts": len(tracked) - len(live),
        "vs_mean": (sum(vs_vals) / len(vs_vals)) if vs_vals else None,
        "total_value": sum(r.get("product_value_krw") or 0 for r in visible
                           if r.get("status") == "진행 중"),
    }

    return tpl.render(
        groups=groups,
        kpi=kpi,
        flags=flags,
        digest=digest,
        agg_brand=_agg(visible, _primary_brand),
        agg_manager=_agg(visible, lambda r: r.get("manager")),
        generated_label=generated_at.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
    )
