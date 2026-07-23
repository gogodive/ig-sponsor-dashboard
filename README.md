# ig-sponsor-dashboard — 협찬 인플루언서 성과 분석기

노션 **[통합] 인플루언서 협찬 DB**의 협찬 건을 매일 추적해, 협찬 게시물이
그 계정의 **평소 대비** 잘 됐는지 · **팔로워 대비** 참여율이 적절한지 ·
**상품 가액 대비** 효율적인지 분석하고 GitHub Pages 대시보드로 발행한다.

- 대시보드: https://gogodive.github.io/ig-sponsor-dashboard/
- 매일 06:00 KST 자동 실행 (GitHub Actions, 로컬 맥 불필요)
- 결과물 URL이 입력된 협찬 건만 대시보드 카드로 표시 (계정은 최신 게시물 순 정렬)

## 파이프라인

```
노션 협찬 DB (진행 중·종료 행)
  └ 행 페이지 안 인라인 "결과물" DB → 협찬 게시물 URL (피드/릴스)
→ Apify instagram-scraper: 계정 최근 30개 + 팔로워 (공개 지표만)
→ 베이스라인: 협찬·고정핀 제외 같은 유형 최근 12개 중앙값
→ 지표: ER(참여/팔로워) · 평소대비(vs 중앙값) · 참여당/조회당 비용
→ 게시 후 30일까지 일별 추적, 이후 동결(확정) + Claude 최종 평가
→ 노션 기입: 결과물 행 반응도·반응체크일, 메인 행 반응도(점수)
→ site/index.html 렌더 → GitHub Pages 배포
```

### 반응도 점수 해석
- 결과물 행 `반응도`(텍스트): `조회 12,345 · 좋아요 678 · 댓글 90 · ER 3.4% · 평소대비 2.1x (D+14)`
- 메인 행 `반응도`(숫자) = 그 협찬 건 게시물들의 평소대비 평균 × 100.
  **100 = 그 계정 평소 수준, 200 = 평소의 2배, 50 = 평소의 절반.**

### 수집 한계
타사 계정은 공개 지표(조회수·좋아요·댓글·팔로워)만 수집 가능.
저장·공유·도달은 계정 주인만 볼 수 있어 어떤 방법으로도 수집 불가.

## 마케터가 지켜야 할 것

행 페이지의 **결과물 DB에 게시물 URL을 입력**해야 추적이 시작된다.
진행 중 상태로 배송일+7일이 지나도 URL이 없으면 대시보드 ⚠️ 패널에 표시된다.

## 실행

```bash
python -m src.main                  # 전체
python -m src.main --dry-run        # 노션 기입 생략
python -m src.main --only user1     # 특정 계정만
python -m src.main --skip-analysis  # Claude 분석 생략
pytest                              # 단위 테스트
```

필요 환경변수(로컬)/Secrets(GitHub): `NOTION_TOKEN` `APIFY_TOKEN` `ANTHROPIC_API_KEY`
— ig-ref-dashboard 와 동일한 값. 노션 통합이 `[통합] 인플루언서 협찬 DB` 페이지에
연결돼 있어야 한다 (행 안의 결과물 DB까지 상속됨).

## 데이터

- `data/sponsorships/{page_id}.json` — 협찬 행 단위 상태 (게시물별 지표·일별 history·분석 캐시)
- `data/accounts/{username}.json` — 계정 단위 최근 게시물·팔로워 스냅샷 (베이스라인용, 매일 덮어씀)
- 종료 + 전부 동결된 행은 API 호출을 생략하므로 DB가 커져도 일일 비용이 늘지 않는다.
